#!/usr/bin/env bash
#
# update_framework.sh — bring a Shared Memory deployment forward to the code
# in this checkout, and prove the result.
#
#   bash shared-memory/scripts/update_framework.sh              # upgrade in place
#   bash shared-memory/scripts/update_framework.sh --from-restore
#   bash shared-memory/scripts/update_framework.sh --dry-run    # print, run nothing
#
# Env overrides: GATEWAY_URL, GATEWAY_UNIT, GATEWAY_RESTART_CMD.
#
# WHY THIS SCRIPT EXISTS. Install had four scripts, the client had two, and the
# framework upgrade had NONE — eight commands of prose in AGENTS.md, including
# an ordering guard whose violation blanks the content of every record it
# touches. Prose is not a procedure: it cannot refuse, and it cannot be run.
#
# ⭐ TWO ENTRY POINTS, ONE PROCEDURE. "Upgrade" is new CODE arriving at existing
# DATA. "Restore" is existing DATA arriving at running CODE. The work in between
# is identical, which is why --from-restore is a flag and not a second script:
# it skips step 0 (fetching code) because restore.sh has just supplied the data
# instead. Every guard below applies equally to both.
#
# ⭐ THE DATABASE STATES ITS OWN LEVEL. `schema_migrations` is a table INSIDE the
# database (apply.py's docstring), and `pg_dump -Fc` carries it, so a restored
# database announces exactly how far it got with no help from a manifest field,
# a version stamp, or anything else that could disagree with the schema it
# claims to describe. Nothing here reads a version out of a backup manifest —
# that value would be DERIVED, and a second source of truth that can drift.
#
# Exit 0 only when postflight passes. Any refusal exits non-zero having changed
# as little as possible.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8888}"
GATEWAY_UNIT="${GATEWAY_UNIT:-hive-mind-gateway.service}"
# How this host restarts the gateway. systemd --user is what install_service.sh
# sets up and what AGENTS.md documents, so it is the DEFAULT — not an assumption
# baked into a code path. A deployment that supervises the gateway some other way
# (a different init, a container, a bare process under a terminal multiplexer)
# overrides this rather than editing the script.
GATEWAY_RESTART_CMD="${GATEWAY_RESTART_CMD:-systemctl --user restart $GATEWAY_UNIT}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

FROM_RESTORE=0
DRY_RUN=0
SKIP_BACKUP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-restore) FROM_RESTORE=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        --skip-backup)  SKIP_BACKUP=1; shift ;;
        -h|--help)      sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)              die "unknown argument: $1" ;;
    esac
done

step=0
run() {
    step=$((step + 1))
    echo
    ylw "── Step $step: $1"
    shift
    printf '   %s\n' "$*"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "   (dry run — not executed)"
        return 0
    fi
    "$@"
}

echo "Shared Memory — framework update"
echo "  repo    : $REPO_ROOT"
echo "  env     : $ENV_FILE"
echo "  gateway : $GATEWAY_URL"
[[ "$FROM_RESTORE" == "1" ]] && echo "  mode    : POST-RESTORE (code is already correct; data has just arrived)"
[[ "$DRY_RUN" == "1" ]]     && echo "  mode    : DRY RUN — nothing is executed"
echo

[[ -f "$ENV_FILE" ]] || die "no .env found — this is not a configured deployment"

# ── Step 0: fetch the new code (upgrade entry point only) ────────────────────
#
# Skipped after a restore: the checkout is already the code we intend to run,
# and the data is what moved. A `git pull` here would be a second, unrelated
# change landing in the middle of a migration — exactly what makes a failure
# impossible to attribute.
#
# ⚠ A tarball install has no repository. AGENTS.md records the route per host at
# the top of .env; this refuses rather than guessing, because `git pull` in a
# directory that was never a checkout fails in a way that reads as broken tooling
# rather than as the wrong procedure. (Measured: it failed on a detached HEAD.)
if [[ "$FROM_RESTORE" == "0" ]]; then
    if [[ -d "$REPO_ROOT/.git" ]]; then
        branch="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD || true)"
        if [[ -z "$branch" ]]; then
            die "this checkout is on a DETACHED HEAD — 'git pull' has no branch to
  update. Check out the release branch or tag you intend to run, then re-run.
  (Or use the tarball route and re-run with --from-restore semantics.)"
        fi
        run "fetch new code (branch: $branch)" git -C "$REPO_ROOT" pull --ff-only
    else
        die "no .git here — this host took the TARBALL route. Unpack the new
  tag's tarball beside this tree, carry shared-memory/.env across, and run this
  script from the NEW directory. There is nothing for 'git pull' to do."
    fi
fi

# ── Step 1: safeguard the data BEFORE any migration touches it ───────────────
#
# A migration is the one step here that cannot be undone by re-running anything.
# The backup is taken through the shipped ops script so it is the SAME artifact
# restore.sh knows how to read — a bespoke dump taken here would be a second
# backup format nobody has ever restored.
#
# ⚠ Retention prunes by AGE (backup.sh: find -mtime +BACKUP_RETENTION_DAYS), so
# this safeguard set is NOT protected from a later prune. Pinning is a separate,
# unbuilt unit; until it exists, copy the set aside if you need it past the
# retention window.
if [[ "$SKIP_BACKUP" == "0" && "$FROM_RESTORE" == "0" ]]; then
    if [[ -f "$REPO_ROOT/shared-memory/ops/backup.sh" ]]; then
        run "backup BEFORE migrating (quiesced, via ops/backup.sh)" \
            bash "$REPO_ROOT/shared-memory/ops/backup.sh" \
            || die "backup failed — refusing to migrate unprotected data.
  Fix the backup, or re-run with --skip-backup if you have a current set already."
    else
        die "ops/backup.sh not found — refusing to migrate
  unprotected data. Re-run with --skip-backup only if a current backup exists."
    fi
else
    step=$((step + 1))
    echo; ylw "── Step $step: backup BEFORE migrating"
    if [[ "$FROM_RESTORE" == "1" ]]; then
        echo "   SKIPPED — post-restore. The dump you just restored IS the safeguard set."
    else
        echo "   SKIPPED — --skip-backup given. You are asserting a current backup exists."
    fi
fi

# ── Step 2: Postgres — forward-only, ledger-driven ───────────────────────────
#
# apply.py resumes from what the database itself records. It now REFUSES (exit 3)
# a database whose ledger names migrations this checkout does not contain — the
# restore-onto-older-code case, which used to report "Up to date" at a filename
# this code has never seen.
run "Postgres migrations (apply.py — forward-only)" \
    uv run --with psycopg2-binary python "$REPO_ROOT/shared-memory/migrations/apply.py"
rc=$?
if [[ "$DRY_RUN" == "0" && "$rc" == "3" ]]; then
    die "the database is AHEAD of this checkout (apply.py exit 3, message above).
  Nothing was migrated. Update the CHECKOUT to a release containing those
  migrations and re-run — the schema cannot be moved backwards."
fi
[[ "$DRY_RUN" == "0" && "$rc" != "0" ]] && die "apply.py failed (exit $rc) — stopping before the graph half."

# ── Step 3: Neo4j — the graph's ENTIRE forward-migration ─────────────────────
#
# ⛔ Neo4j has NO ledger. neo4j_init.cypher is a one-time manual step, so a
# long-lived instance enforces whatever constraint set was true the day someone
# last ran it, and a constraint added in a later release reaches new installs and
# nobody else. A missing uniqueness constraint is SILENT — MERGE keeps working
# and the only symptom is a duplicate graph node under a race. This is not optional and
# not redundant with apply.py, which cannot reach Neo4j at all.
run "Neo4j constraints (no ledger exists — verify every time)" \
    uv run --with neo4j python "$REPO_ROOT/shared-memory/migrations/verify_neo4j_init.py" --apply
rc=$?
[[ "$DRY_RUN" == "0" && "$rc" != "0" ]] && die "Neo4j constraint check FAILED (exit $rc) even with --apply.
  A declared constraint is not in force and could not be created — commonly a
  plain index blocking a uniqueness constraint. Stopping BEFORE the restart:
  a missing uniqueness constraint is silent, and MERGE keeps working."

# ── Step 4: the graph half of migration 027 ──────────────────────────────────
#
# apply.py creates the registry ids and cannot reach Neo4j, so this stamps the
# :Project nodes. Skipping it does not break writes — records still save, search
# and enrich. What stops is CROSS-PROJECT SYNTHESIS: the fold gate fails closed
# on any node lacking an identity, which presents as a system with nothing to fold
# rather than as an error. Idempotent; read-only without --apply.
run "stamp project identity onto :Project nodes (graph half of migration 027)" \
    uv run --with psycopg2-binary --with neo4j python \
    "$REPO_ROOT/shared-memory/scripts/reconcile_project_identity.py" --apply
rc=$?
[[ "$DRY_RUN" == "0" && "$rc" != "0" ]] && die "project identity reconcile FAILED (exit $rc).
  Writes would still work, so this is easy to wave through — but cross-project
  synthesis fails CLOSED on unidentified nodes and presents as a quiet corpus
  rather than an error. Fix it here, where it is still visible."

# ── Step 5: restart, so the running gateway IS the migrated code ─────────────
# The restart is the hinge: every step after it assumes the running process IS
# the migrated code. Refuse early and clearly rather than emitting
# "systemctl: command not found" from the middle of a migration.
if [[ "$DRY_RUN" == "0" && "$GATEWAY_RESTART_CMD" == systemctl* ]] \
   && ! command -v systemctl >/dev/null 2>&1; then
    die "this host has no systemctl, and GATEWAY_RESTART_CMD was left at its
  systemd default. The schema is migrated; the gateway is NOT yet restarted.
  Set GATEWAY_RESTART_CMD to whatever restarts the gateway here and re-run —
  the migration steps above are idempotent, so re-running is safe."
fi
run "restart the gateway" bash -c "$GATEWAY_RESTART_CMD"

if [[ "$DRY_RUN" == "0" ]]; then
    echo "   waiting for the gateway to answer ..."
    for _ in $(seq 1 30); do
        curl -sf --max-time 2 "$GATEWAY_URL/health" >/dev/null 2>&1 && break
        sleep 1
    done
    curl -sf --max-time 5 "$GATEWAY_URL/health" >/dev/null 2>&1 \
        || die "gateway did not come back after restart — check: journalctl --user -u $GATEWAY_UNIT -n 50"
    grn "   ✓ gateway responding: $(curl -s --max-time 5 "$GATEWAY_URL/health")"
fi

# ── Step 6: domain backfill — AFTER the restart, and that is a GUARD ──────────
#
# ⛔ ORDERING IS SAFETY, NOT PREFERENCE. This enqueues a narrow repair row that
# only a gateway from v0.8.47 understands. An OLDER worker does not recognise the
# row type, falls through to its ordinary fact branch, and BLANKS THE CONTENT of
# every record it touches. Running it before the restart means enqueuing work for
# the process you are about to replace. The script refuses a gateway that is too
# old — including one it cannot reach, because an unknown version is not
# permission to write — so early is safe but pointless, and this ordering makes
# "safe but pointless" into "correct".
#
# Dry run first so the operator sees what WOULD be enqueued, then apply.
run "domain backfill — preview (enqueues nothing)" \
    uv run --with psycopg2-binary python \
    "$REPO_ROOT/shared-memory/scripts/backfill_domain_of.py"
run "domain backfill — apply" \
    uv run --with psycopg2-binary python \
    "$REPO_ROOT/shared-memory/scripts/backfill_domain_of.py" --apply

# ── Step 7: refresh the installed client skills ──────────────────────────────
#
# ⚠ AFTER the restart, deliberately. Run before it, update_skill.sh compares the
# new client against the OLD gateway and prints "Updated to X but still
# incompatible. The GATEWAY itself ..." — alarming, self-resolving one step
# later, and observed on two hosts. Ordering removes the false alarm rather than
# rewording it.
run "refresh installed agent skills" bash "$REPO_ROOT/shared-memory/scripts/sync_skills.sh"
rc=$?
if [[ "$DRY_RUN" == "0" && "$rc" != "0" ]]; then
    # Not fatal: the gateway is already migrated and correct. But never silent —
    # skills are shipped as COPIES, and a stale copy fails silently forever.
    ylw "   ! sync_skills.sh exited $rc — installed client skills may be STALE."
    ylw "     Re-run it by hand and check each agent's version before trusting them."
fi

# ── Step 8: prove it ─────────────────────────────────────────────────────────
#
# ⚠ postflight NEEDS AGENT_TOKEN EXPORTED or A1/A5/A8 skip and it exits 1. That
# is documented behaviour, not a defect — but it is also the single most common
# way this step "fails" for a reason that has nothing to do with the update.
if [[ "$DRY_RUN" == "0" && -z "${AGENT_TOKEN:-}" ]]; then
    ylw "   ! AGENT_TOKEN is not exported — postflight's A1/A5/A8 will SKIP and it"
    ylw "     will exit 1. Export an agent token and run postflight yourself:"
    ylw "       AGENT_TOKEN=<token> bash shared-memory/scripts/postflight.sh"
    echo
    ylw "Update finished, but UNVERIFIED. An update is not complete until postflight passes."
    exit 1
fi
run "postflight — verify end to end" bash "$REPO_ROOT/shared-memory/scripts/postflight.sh"
rc=$?

echo
if [[ "$DRY_RUN" == "1" ]]; then
    grn "Dry run complete — nothing was executed."
elif [[ "$rc" == "0" ]]; then
    grn "Update complete and VERIFIED — postflight passed."
    ylw "Recommended: take a second backup now, so a known-good set exists at the new level."
else
    die "postflight FAILED (exit $rc). The code and schema have moved; the system is
  NOT verified. Read the failures above before using this deployment."
fi
