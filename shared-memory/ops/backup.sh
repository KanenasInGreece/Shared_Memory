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
    -h|--help)     awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
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
# The framework's own logs: credential + gateway audit trails, dreaming metrics,
# daily logs. Captured by default — they exist nowhere else, and the monitor
# reads this directory to surface warnings.
BACKUP_INCLUDE_LOGS="${BACKUP_INCLUDE_LOGS:-1}"
LOG_DIR="${SHARED_MEMORY_LOG_DIR:-$HOME/.shared-memory/logs}"
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
# miss/parse error, OR on a key that is genuinely absent — robust to an empty
# or non-JSON response (no stack trace) AND indistinguishable from "this
# manifest predates the field", which is deliberate: callers must treat
# absence as unknown, never coerce it to a false/zero value. bool is a
# subclass of int in Python, so it is special-cased to "true"/"false" text —
# without this, a JSON `false` would print as the ambiguous Python "False".
json_get() { python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d=d.get(k,{}) if isinstance(d,dict) else {}
    if isinstance(d, bool):
        print(str(d).lower())
    else:
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

# C1 + QF-1 (fix round 1): BACKUP_ADMIN_TOKEN and BOTH database passwords off
# argv AND off the docker CLIENT's own argv on the GATEWAY HOST.
#
# QF-1 (why this is no longer a function called from inside $(...)): the
# original cut called `auth_header_file()` only as `-H "@$(auth_header_file)"`
# — command substitution runs in a SUBSHELL, so the function's variable
# assignments (_AUTH_HEADER_DIR/_AUTH_HEADER_FILE) never reached the PARENT
# shell. Every call therefore minted a NEW tmpdir holding the token, the
# parent's own copies of those variables stayed empty forever, and the exit
# trap — which only ever saw the parent's empty variables — cleaned up
# NOTHING. Token files leaked into /tmp indefinitely after every run. Fixed
# by creating everything EAGERLY, as a plain statement (never inside
# `$(...)`), so the assignment lands in the shell that will later run the
# cleanup trap.
#
# C1 (why docker exec -e is gone): `-e KEY=VALUE` is an argument to the
# `docker` CLIENT process running on the GATEWAY HOST — `ps aux` (or
# /proc/<pid>/cmdline, world-readable on a host with no hidepid) shows it to
# every same-uid process, and on this host to every user, for the whole
# `docker exec` lifetime (minutes, for pg_dump). `docker exec --env-file
# <file>` reads KEY=VALUE lines from a file instead — confirmed present on
# this host's docker (`docker exec --help` → "--env-file list  Read in a
# file of environment variables"). It carries NEO4J_PASSWORD/PGPASSWORD
# straight into the container's env with no host-side argv exposure at all.
#
# ONE private 0700 mktemp -d directory holds all three files (the curl auth
# header, and one --env-file each for Postgres/Neo4j) — one dir, one trap
# cleanup, created once per run. init_secrets_dir() is idempotent (a second
# call is a no-op) and MUST be called as a bare statement, never inside
# `$(...)`, or QF-1 recurs.
_SECRETS_DIR=""
_AUTH_HEADER_FILE=""
_NEO4J_ENV_FILE=""
_PG_ENV_FILE=""
init_secrets_dir() {
  [[ -n "$_SECRETS_DIR" ]] && return 0
  _SECRETS_DIR="$(mktemp -d)"
  chmod 700 "$_SECRETS_DIR"
  _AUTH_HEADER_FILE="$_SECRETS_DIR/auth-header"
  _NEO4J_ENV_FILE="$_SECRETS_DIR/neo4j.env"
  _PG_ENV_FILE="$_SECRETS_DIR/pg.env"
  printf 'Authorization: Bearer %s\n' "$BACKUP_ADMIN_TOKEN" > "$_AUTH_HEADER_FILE"
  printf 'NEO4J_PASSWORD=%s\n' "$NEO4J_PASSWORD" > "$_NEO4J_ENV_FILE"
  printf 'PGPASSWORD=%s\n' "$PG_PASSWORD" > "$_PG_ENV_FILE"
  chmod 600 "$_AUTH_HEADER_FILE" "$_NEO4J_ENV_FILE" "$_PG_ENV_FILE"
}
cleanup_secrets_dir() {
  [[ -n "$_SECRETS_DIR" ]] && rm -rf "$_SECRETS_DIR"
  _SECRETS_DIR=""; _AUTH_HEADER_FILE=""; _NEO4J_ENV_FILE=""; _PG_ENV_FILE=""
}

# NEW-5 (fix round 2, probe-confirmed): QUIESCED must be defined BEFORE the
# trap is installed below, not after. Under `set -u` (line 28), a signal
# landing in the window between `trap on_exit …` and a later `QUIESCED=0`
# would make on_exit()'s call to resume() read an UNBOUND variable
# (`[[ "$QUIESCED" -eq 1 ]]`) and abort — inside a trap handler, on the
# exact path meant to release the gateway's quiesce fence. Every other
# variable on_exit()'s path reads (GATEWAY_URL, BACKUP_ADMIN_TOKEN, and
# _SECRETS_DIR/_AUTH_HEADER_FILE/_NEO4J_ENV_FILE/_PG_ENV_FILE above) is
# already defined earlier in the script — QUIESCED was the one gap.
QUIESCED=0
# Set alongside QUIESCED, by quiesce() below, so the manifest can record HOW
# a quiesced backup was quiesced: "full" (200 — daemons drained cleanly) or
# "timeout" (202 — the drain wait ran out; a daemon MAY have written during
# the dump). Empty when QUIESCED=0 (quiesce never engaged at all). resume()
# does not touch this — do_backup reads it before resume() runs, same as it
# must read QUIESCED itself before resume() zeroes that.
QUIESCE_MODE=""
# Set by quiesce() on every path that returns 1 (skipped/failed), so the
# "proceeding WITHOUT quiesce" message can say WHICH of the three distinct
# reasons it was — measured on a documented upgrade run: the message used to
# fold "gateway unreachable" and "no admin token" into one string, and an
# operator reading it afterward could not tell which had actually happened,
# or whether they had a token at all vs. one that was simply rejected.
# Declared here (defined before the trap, `set -u`) for the same reason
# QUIESCED/QUIESCE_MODE are — do_backup reads it, never on_exit, but keeping
# every variable that flow can reach defined this early costs nothing.
QUIESCE_SKIP_REASON=""

# One trap, active for the whole script (not just do_backup): resume() is a
# no-op unless quiesce() actually succeeded (QUIESCED guard), and
# cleanup_secrets_dir is a no-op unless init_secrets_dir ever ran — so this
# is safe to install unconditionally, before mode dispatch, and covers
# --dry-run too (which also touches docker).
on_exit() { resume; cleanup_secrets_dir; }
trap on_exit EXIT INT TERM

# ── Quiesce handshake ────────────────────────────────────────────────────────
# Three DISTINCT ways this can fail to engage, each needing a different
# operator fix — quiesce() names which one in QUIESCE_SKIP_REASON before
# returning 1, so do_backup's "proceeding WITHOUT quiesce" message can say it:
#   (1) no admin token           — BACKUP_ADMIN_TOKEN was never set
#   (2) gateway unreachable      — the curl call itself failed (down host,
#                                   wrong GATEWAY_URL, network)
#   (3) token lacks the role     — a token WAS presented and the gateway WAS
#                                   reachable, but it answered 401 (rejected/
#                                   expired) or 403 (valid, not admin-role)
# >>> QUIESCE_FN
quiesce() {
  if [[ -z "$BACKUP_ADMIN_TOKEN" ]]; then
    QUIESCE_SKIP_REASON="no admin token — BACKUP_ADMIN_TOKEN is unset; mint one with bootstrap_tokens.sh --add (role=admin) and set it in shared-memory/.env"
    return 1
  fi
  local resp code body
  if ! resp="$(qcurl -w $'\n%{http_code}' -X POST "$GATEWAY_URL/admin/backup" \
    -H "@$_AUTH_HEADER_FILE" -H 'Content-Type: application/json' \
    -d "{\"state\":\"quiesce\",\"max_seconds\":$BACKUP_QUIESCE_MAX_SECONDS}")"; then
    QUIESCE_SKIP_REASON="gateway unreachable at $GATEWAY_URL — check it is running (systemctl --user status hive-mind-gateway.service) and that GATEWAY_URL is correct"
    return 1
  fi
  code="$(tail -n1 <<<"$resp")"; body="$(sed '$d' <<<"$resp")"
  case "$code" in
    200) QUIESCED=1; QUIESCE_MODE="full";    grn "  ✓ quiesced — client writes shed, daemons drained" ;;
    202) QUIESCED=1; QUIESCE_MODE="timeout"; ylw "  ! quiesced — daemon drain TIMED OUT; a daemon may write during the dump ($(json_get daemons <<<"$body"))" ;;
    401) QUIESCE_SKIP_REASON="BACKUP_ADMIN_TOKEN was rejected (HTTP 401 — invalid or expired); re-mint it with bootstrap_tokens.sh"; return 1 ;;
    403) QUIESCE_SKIP_REASON="BACKUP_ADMIN_TOKEN lacks the admin role (HTTP 403) — re-run bootstrap_tokens.sh --add with role=admin for this token"; return 1 ;;
    "")  QUIESCE_SKIP_REASON="gateway unreachable at $GATEWAY_URL (no HTTP response)"; return 1 ;;
    *)   QUIESCE_SKIP_REASON="quiesce request returned HTTP $code from $GATEWAY_URL"; ylw "  ! quiesce request returned HTTP $code"; return 1 ;;
  esac
  return 0
}
# <<< QUIESCE_FN
resume() {
  [[ "$QUIESCED" -eq 1 ]] || return 0
  dcurl -X POST "$GATEWAY_URL/admin/backup" \
    -H "@$_AUTH_HEADER_FILE" -H 'Content-Type: application/json' \
    -d '{"state":"resume"}' >/dev/null 2>&1
  QUIESCED=0
}

# Wait until the outbox is empty (pending==0 && in_progress==0) so Neo4j is caught
# up with Postgres before the snapshot. Best-effort: proceeds on timeout.
drain_outbox() {
  local waited=0 pend prog
  while (( waited < BACKUP_DRAIN_MAX_SECONDS )); do
    local tel; tel="$(dcurl "$GATEWAY_URL/memory/telemetry" \
      -H "@$_AUTH_HEADER_FILE")" || break
    pend="$(json_get telemetry.postgres.outbox.pending     <<<"$tel")"; pend="${pend:-0}"
    prog="$(json_get telemetry.postgres.outbox.in_progress <<<"$tel")"; prog="${prog:-0}"
    [[ "$pend" == "0" && "$prog" == "0" ]] && { grn "  ✓ outbox drained"; return 0; }
    sleep "$BACKUP_DRAIN_POLL_SECONDS"; waited=$(( waited + BACKUP_DRAIN_POLL_SECONDS ))
  done
  ylw "  ! outbox not fully drained after ${BACKUP_DRAIN_MAX_SECONDS}s — proceeding (restore self-heals on replay)"
}

# ── Dump primitives ──────────────────────────────────────────────────────────
# C1 (fix round 1): NEITHER password is on the docker CLIENT's own argv.
# `docker exec -e KEY=VALUE` (the PR's original "fix") is an argument to the
# `docker` process running on the GATEWAY HOST — that IS argv, visible to
# `ps aux` for the whole `docker exec` lifetime, regardless of what
# cypher-shell/pg_dump themselves do with it once inside the container. Every
# site below now uses `--env-file "$_NEO4J_ENV_FILE"` /
# `--env-file "$_PG_ENV_FILE"` instead — a 600-mode file under the private
# secrets dir `init_secrets_dir()` creates, read by the docker CLIENT itself,
# never placed on its own command line. `-u`/NEO4J_USER stays on argv — a
# username is not a secret. (Still true, unaffected by this fix: cypher-shell
# itself takes no `-p`, reading NEO4J_PASSWORD from the CONTAINER's env,
# which --env-file populates exactly like -e did.)
neo4j_count() {  # $1 = cypher count query → integer (or empty)
  $DOCKER exec --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" --format plain "$1" 2>/dev/null | tail -n1
}

dump_postgres() {  # $1 = dest path
  $DOCKER exec --env-file "$_PG_ENV_FILE" "$PG_CONTAINER" \
    pg_dump -U "$PG_USER" -Fc "$PG_DB" > "$1.tmp" 2>/dev/null \
    && mv "$1.tmp" "$1" || { rm -f "$1.tmp"; return 1; }
}

# List a .pgdump archive's table-of-contents entry count via the CONTAINER's
# pg_restore. This does NOT run pg_restore on the host: Postgres runs only in
# $PG_CONTAINER, the host carries no postgres client tools, and the previous
# form of this check (`command -v pg_restore` on the host) always failed —
# silently falling through to a hardcoded 0 in every manifest this framework
# has ever written (measured: a real set's manifest said 0, listing the same
# dump inside the container reported 189). The archive lives on the HOST
# filesystem (dump_postgres streams pg_dump's stdout out of the container to
# a host path), so it is piped IN via stdin (`docker exec -i`) rather than
# passed as a path pg_restore would have to find inside the container.
# --list only parses the archive's header/TOC — it opens no database
# connection and needs no credentials, so this needs no --env-file, unlike
# every other Postgres call in this script.
pgdump_toc() {  # $1 = path to a .pgdump file → TOC entry count on stdout, or empty on failure
  $DOCKER exec -i "$PG_CONTAINER" pg_restore --list < "$1" 2>/dev/null | grep -cvE '^;|^$'
}

# Resolve Neo4j's configured import dir (where APOC writes) so we read the export
# from the right place regardless of the deployment's server.directories.import.
resolve_import_dir() {
  local d
  d="$($DOCKER exec --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" --format plain \
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
  $DOCKER exec --env-file "$_NEO4J_ENV_FILE" "$NEO4J_CONTAINER" \
    cypher-shell -u "$NEO4J_USER" --format plain \
    "CALL apoc.export.cypher.all('$fname', {format:'cypher-shell'}) YIELD file RETURN file" \
    >/dev/null 2>&1 || return 1
  $DOCKER exec "$NEO4J_CONTAINER" cat "$idir/$fname" 2>/dev/null | gzip > "$1.tmp" || { rm -f "$1.tmp"; return 1; }
  $DOCKER exec "$NEO4J_CONTAINER" rm -f "$idir/$fname" >/dev/null 2>&1
  [[ -s "$1.tmp" ]] || { rm -f "$1.tmp"; return 1; }
  mv "$1.tmp" "$1"
}

# ── Modes ────────────────────────────────────────────────────────────────────
do_dry_run() {
  init_secrets_dir   # bare statement, not $(...) — see the QF-1 comment above
  echo "Shared Memory — backup DRY RUN (no writes, no quiesce)"; echo
  local pgsize nodes rels
  pgsize="$($DOCKER exec --env-file "$_PG_ENV_FILE" "$PG_CONTAINER" \
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
  local logs_sha_m; logs_sha_m="$(json_get logs_sha256 < "$manifest")"
  if [[ -n "$logs_sha_m" ]]; then
    if [[ ! -f "$base.logs.tar.gz" ]]; then
      red "  ✗ manifest claims a logs artifact but $base.logs.tar.gz is missing"; fail=1
    elif [[ "$(sha256sum "$base.logs.tar.gz" | awk '{print $1}')" == "$logs_sha_m" ]]; then
      grn "  ✓ logs archive sha256 OK"
    else
      red "  ✗ logs archive sha256 MISMATCH"; fail=1
    fi
  else
    # Absence is normal: every set written before logs were captured has none.
    echo "  i no logs artifact in this set (predates log capture, or disabled)"
  fi

  # pg_restore --list runs via the CONTAINER, same as the manifest's own
  # pg_toc_entries computation (pgdump_toc, above) — the host carries no
  # postgres client tools, so a host-side `command -v pg_restore` check here
  # always failed and this archive-readable check silently never ran on any
  # of our own installs. Re-derive the count live and, when the manifest
  # carries one (a manifest written before this fix has none — absence is
  # unknown, never treated as a mismatch), cross-check it: a live count that
  # disagrees with what was recorded at backup time is a real integrity
  # signal now that both sides come from the same tool.
  local live_toc manifest_toc
  live_toc="$(pgdump_toc "$base.pgdump")"
  manifest_toc="$(json_get pg_toc_entries < "$manifest")"
  if [[ "$live_toc" =~ ^[0-9]+$ ]]; then
    # A recorded "0" is not a genuinely observed zero-entry archive — it is
    # the ONLY value the pre-fix host-side check could ever write (`command
    # -v pg_restore` always failed on the host, so the `|| echo 0` branch
    # always fired), so it means the same thing absence does: this manifest
    # predates a working count. Treating it as a real recorded value would
    # turn every backup set made before this fix into a false MISMATCH.
    if [[ -n "$manifest_toc" && "$manifest_toc" != "0" ]]; then
      if [[ "$live_toc" == "$manifest_toc" ]]; then
        grn "  ✓ pgdump archive readable, TOC entries $live_toc (matches manifest)"
      else
        red "  ✗ pgdump TOC entries $live_toc, manifest recorded $manifest_toc — MISMATCH"; fail=1
      fi
    else
      grn "  ✓ pgdump archive readable via container pg_restore, TOC entries $live_toc (manifest predates a working count — nothing to cross-check)"
    fi
  else
    red "  ✗ pgdump archive unreadable (container pg_restore --list failed)"; fail=1
  fi

  # 3. quiesce state — informational only, never fails verification (an
  # unquiesced backup is a valid, restorable backup; restore.sh's own
  # closing message already says a count mismatch "can be normal if the
  # backup ran without full quiesce" — this makes that possibility visible
  # up front instead of only after a confusing post-restore count).
  local quiesced quiesce_mode
  quiesced="$(json_get quiesced < "$manifest")"
  quiesce_mode="$(json_get quiesce_mode < "$manifest")"
  case "$quiesced" in
    true)  grn "  i quiesced at backup time (${quiesce_mode:-full drain})" ;;
    false) ylw "  i NOT quiesced at backup time — a daemon may have written during the dump" ;;
    *)     ylw "  i quiesce state unknown (manifest predates this field)" ;;
  esac

  echo
  [[ "$fail" -eq 0 ]] && { grn "Backup set VERIFIED."; return 0; } || die "Backup set FAILED verification."
}

do_backup() {
  init_secrets_dir   # bare statement, not $(...) — see the QF-1 comment above;
                     # the script-wide `trap on_exit` already installed covers
                     # cleanup, nothing further to install here.
  mkdir -p "$BACKUP_DIR"
  local ts name base
  ts="$(date +%Y%m%d-%H%M%S)"; name="${PREFIX}-${ts}"; base="$BACKUP_DIR/$name"
  echo "Shared Memory — backup → $base.*"

  # Quiesce (best-effort unless required).
  if quiesce; then drain_outbox
  else
    if [[ "$BACKUP_QUIESCE_REQUIRED" == "1" ]]; then die "quiesce required but unavailable: ${QUIESCE_SKIP_REASON:-reason unknown}"; fi
    ylw "  ! proceeding WITHOUT quiesce (${QUIESCE_SKIP_REASON:-reason unknown}) — online dumps, restore self-heals"
  fi

  # Counts captured under quiesce for the manifest.
  local nodes rels; nodes="$(neo4j_count 'MATCH (n) RETURN count(n)')"; rels="$(neo4j_count 'MATCH ()-[r]->() RETURN count(r)')"

  # Capture the quiesce state for the manifest BEFORE resume() zeroes QUIESCED
  # (QUIESCE_MODE is untouched by resume(), but read it here too so both
  # values are snapshotted together at the same point in the flow).
  local was_quiesced="$QUIESCED" quiesce_mode="$QUIESCE_MODE"

  # Dump Postgres FIRST (source of truth + outbox), then Neo4j.
  dump_postgres "$base.pgdump"   || die "pg_dump failed"
  grn "  ✓ postgres dumped"
  dump_neo4j    "$base.cypher.gz" || die "neo4j APOC export failed (is NEO4J_apoc_export_file_enabled=true?)"
  grn "  ✓ neo4j dumped"

  resume   # release the quiesce as early as possible — dumps are done

  # Manifest LAST — its presence marks the set complete (verify keys off it).
  local pg_sha neo_sha pg_toc
  # ── The framework logs are the FOURTH artifact ─────────────────────────────
  #
  # A set used to be exactly the two stores, which meant a restored host came up
  # with the corpus and NO operational history: the credential audit trail, the
  # gateway audit trail, the dreaming metrics and the daily logs all live only in
  # $LOG_DIR and were in no backup. The monitor reads that directory directly
  # (its logs_reader), so a restored deployment showed a healthy corpus and could
  # not surface a single warning — it had nothing to read.
  #
  # 2.4 MB against a 21 MB dump on a real install: the cost is not the reason
  # this was ever left out. Opt out with BACKUP_INCLUDE_LOGS=0 for a deployment
  # that does not want audit trails leaving the host.
  logs_sha=""; logs_bytes=""
  if [[ "$BACKUP_INCLUDE_LOGS" == "1" && -d "$LOG_DIR" ]]; then
    if tar czf "$base.logs.tar.gz" -C "$(dirname "$LOG_DIR")" "$(basename "$LOG_DIR")" 2>/dev/null; then
      logs_sha="$(sha256sum "$base.logs.tar.gz" | awk '{print $1}')"
      logs_bytes="$(stat -c%s "$base.logs.tar.gz" 2>/dev/null || echo '')"
      grn "  ✓ logs captured ($(du -h "$base.logs.tar.gz" | awk '{print $1}'))"
    else
      # Never fatal: the corpus is the thing that cannot be reconstructed. A
      # missing logs artifact is recorded as absent, which restore reads the same
      # way it reads a set written before this existed.
      ylw "  ! could not capture $LOG_DIR — continuing without it"
      rm -f "$base.logs.tar.gz"
    fi
  fi

  pg_sha="$(sha256sum "$base.pgdump"    | awk '{print $1}')"
  neo_sha="$(sha256sum "$base.cypher.gz" | awk '{print $1}')"
  pg_toc="$(pgdump_toc "$base.pgdump")"
  [[ "$pg_toc" =~ ^[0-9]+$ ]] || { ylw "  ! could not list pgdump TOC via the container — recording pg_toc_entries as null (sha256/gzip integrity still validated)"; pg_toc=""; }
  python3 - "$base.manifest.json" <<PY
import json, sys
json.dump({
  "name": "$name", "created": "$(date -Iseconds)",
  "pg_db": "$PG_DB",
  "pg_file": "$name.pgdump", "pg_sha256": "$pg_sha",
  "pg_toc_entries": int("$pg_toc") if "$pg_toc" else None,
  "neo4j_file": "$name.cypher.gz", "neo4j_sha256": "$neo_sha",
  "neo4j_nodes": "${nodes:-}", "neo4j_rels": "${rels:-}",
  "quiesced": bool($was_quiesced),
  "quiesce_mode": "$quiesce_mode" or None,
  "logs_file": "$name.logs.tar.gz" if "$logs_sha" else None,
  "logs_sha256": "$logs_sha" or None,
  "logs_bytes": int("$logs_bytes") if "$logs_bytes" else None,
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
