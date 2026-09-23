#!/usr/bin/env bash
#
# update_framework.sh — bring a Shared Memory deployment forward to the code in this checkout, and prove the result.
#
#   bash shared-memory/scripts/update_framework.sh              # upgrade in place
#   bash shared-memory/scripts/update_framework.sh --from-restore
#   bash shared-memory/scripts/update_framework.sh --dry-run    # print, run nothing
#   bash shared-memory/scripts/update_framework.sh --domain-backfill  # also run step 8 (opt-in)
#   bash shared-memory/scripts/update_framework.sh --skip-env-migration  # see below
#   bash shared-memory/scripts/update_framework.sh --skip-backup  # see below
#
# --skip-backup skips the backup-before-migrate step and asserts you already have a current one. NEVER pass it on a host holding the only copy of the data.
#
# --no-domain-backfill is a one-release no-op so an existing invocation does not break: the domain backfill is opt-in now (fact:1734). Pass --domain-backfill to run it.
#
# --skip-env-migration skips the pre-pull capture and the migrate_env.py --apply step, so a capture bug cannot block a security update. Run migrate_env.py by hand for this upgrade.
#
# Env overrides: GATEWAY_URL, GATEWAY_UNIT, GATEWAY_RESTART_CMD.
#
# --from-restore is the same procedure as an upgrade, minus fetching code, because restore.sh has just supplied the data. Every guard below applies to both.
#
# schema_migrations lives in the database and travels with pg_dump, so a restored database states its own level. This script does not read a version out of a backup manifest.
#
# Exit 0 only when postflight passes. Any refusal exits non-zero having changed as little as possible.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8888}"
GATEWAY_UNIT="${GATEWAY_UNIT:-hive-mind-gateway.service}"
# systemd --user is the default because that is what install_service.sh sets up. Override this instead of editing the script when the gateway is supervised some other way.
GATEWAY_RESTART_CMD="${GATEWAY_RESTART_CMD:-systemctl --user restart $GATEWAY_UNIT}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

# Exit hooks are function names, never a command string passed to eval.
_EXIT_CLEANUP_FUNCS=()
_run_exit_cleanup() {
    local fn
    if [[ ${#_EXIT_CLEANUP_FUNCS[@]} -gt 0 ]]; then
        for fn in "${_EXIT_CLEANUP_FUNCS[@]}"; do
            "$fn" 2>/dev/null || true
        done
    fi
}
add_exit_cleanup() { _EXIT_CLEANUP_FUNCS+=("$1"); trap _run_exit_cleanup EXIT; }

# Under --dry-run a branch-guard refusal is printed as predicted and returned, not exited. A real run still dies. Only step 0 uses this; every other refusal calls die().
refuse() {
    if [[ "$DRY_RUN" == "1" ]]; then
        red "✗ [DRY RUN — PREDICTED: a real run would refuse here] $*"
        return 1
    fi
    die "$*"
}

# A feature branch that still exists on the remote pulls forward and can exit 0 without reaching the release. Repeat the branch at every terminal banner; running a branch on purpose is allowed, hiding it is not.
UPDATE_BRANCH=""
_branch_notice() {
    [[ -n "$UPDATE_BRANCH" && "$UPDATE_BRANCH" != "main" ]] || return 0
    ylw "   ⚠ this checkout is on branch '$UPDATE_BRANCH', not main. Pulling it forward"
    ylw "     moves '$UPDATE_BRANCH' to ITS OWN latest commit — NOT to the latest release."
    ylw "     If you intend to upgrade to the released code, check out main and re-run."
}

# "yes" and "not-applicable" used to print nothing, so a passing linger check looked like one that never ran. "no" stays loud below.
_linger_brief() {
    case "$LINGER_VERDICT" in
        yes)             echo "   ✓ linger check: enabled for $_linger_who." ;;
        not-applicable)  echo "   · linger check: not applicable on this host." ;;
    esac
}

FROM_RESTORE=0
DRY_RUN=0
SKIP_BACKUP=0
DOMAIN_BACKFILL=0
NO_DOMAIN_BACKFILL_NOTICE=0
SKIP_ENV_MIGRATION=0
PREIMAGE_JSON=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-restore)       FROM_RESTORE=1; shift ;;
        --dry-run)            DRY_RUN=1; shift ;;
        --skip-backup)        SKIP_BACKUP=1; shift ;;
        --domain-backfill)    DOMAIN_BACKFILL=1; shift ;;
        --no-domain-backfill) NO_DOMAIN_BACKFILL_NOTICE=1; shift ;;
        --skip-env-migration) SKIP_ENV_MIGRATION=1; shift ;;
        -h|--help)      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
        *)              die "unknown argument: $1" ;;
    esac
done

step=0
# run() dies on failure. It used to return the code, and an unchecked pull, restart, or backfill then continued against old code, including a worker that blanks record content. Use run_soft() only when the caller inspects the code or tolerates failure.
run() {
    local label="$1"; shift
    step=$((step + 1))
    echo
    ylw "── Step $step: $label"
    printf '   %s\n' "$*"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "   (dry run — not executed)"
        return 0
    fi
    "$@" || die "step $step FAILED: $label
  Command: $*
  Nothing after this step has run."
}

# Same output, but returns the exit code instead of dying — for callers that
# distinguish between failure modes themselves.
run_soft() {
    local label="$1"; shift
    step=$((step + 1))
    echo
    ylw "── Step $step: $label"
    printf '   %s\n' "$*"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "   (dry run — not executed)"
        return 0
    fi
    "$@"
}

# Read linger, never enable it. Without the flag, systemd stops the user gateway when the session ends, and a check inside a session cannot see that. install_service.sh owns enabling it.
# The flag is the file /var/lib/systemd/linger/<user>. loginctl is only a fallback when that directory is absent, and "not logged in or lingering" is a real no, not a failure to answer. timeout keeps a wedged D-Bus from stalling the upgrade.
# The directory is a parameter, not an environment variable. An exported LINGER_DIR used to bypass the check on a real run. The function takes no script state so the test can extract it.
# >>> LINGER_CHECK
check_linger() {
    local who="${USER:-$(id -un)}"
    local linger_dir="${1:-/var/lib/systemd/linger}"

    if [[ -d "$linger_dir" ]]; then
        if [[ -e "$linger_dir/$who" ]]; then
            echo "yes"
        else
            echo "no"
        fi
        return 0
    fi

    command -v loginctl >/dev/null 2>&1 || { echo "not-applicable"; return 0; }

    local out rc
    out="$(timeout 5 loginctl show-user "$who" --property=Linger 2>&1)"
    rc=$?

    if [[ "$rc" -eq 0 ]]; then
        if [[ -z "$out" ]]; then
            echo "not-applicable"
        elif echo "$out" | grep -qx "Linger=yes"; then
            echo "yes"
        else
            echo "no"
        fi
        return 0
    fi

    # "is not logged in or lingering" is linger off. Any other nonzero answer is unanswered.
    if echo "$out" | grep -q "is not logged in or lingering"; then
        echo "no"
    else
        echo "not-applicable"
    fi
}
# <<< LINGER_CHECK

# Measure linger before anything can die, so every terminal path already has a verdict. It changes no state, so it is not a numbered step.
_linger_who="${USER:-$(id -un)}"
LINGER_VERDICT="$(check_linger)"

echo "Shared Memory — framework update"
echo "  repo    : $REPO_ROOT"
echo "  env     : $ENV_FILE"
echo "  gateway : $GATEWAY_URL"
[[ "$FROM_RESTORE" == "1" ]] && echo "  mode    : POST-RESTORE (code is already correct; data has just arrived)"
[[ "$DRY_RUN" == "1" ]]     && echo "  mode    : DRY RUN — nothing is executed"
echo

[[ -f "$ENV_FILE" ]] || die "no .env found — this is not a configured deployment"

# Check tools before any fetch or backup. They used to fail at the first uv run, after a pull and a full backup had already happened.
# uv in ~/.local/bin is invisible to a profile-free shell. That is the normal installer outcome, so it is named here.
missing=""
for tool in git uv curl; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [[ -n "$missing" ]]; then
    die "missing on PATH:$missing — nothing has been fetched, dumped or migrated.

  If this shell is non-interactive (ssh, cron, a systemd unit) the tool may be
  installed but unreachable: the upstream uv installer writes ~/.local/bin/uv,
  which only a login shell puts on PATH. Check with:

      ls -l ~/.local/bin/uv

  and if it is there, export PATH=\"\$HOME/.local/bin:\$PATH\" before re-running."
fi

# Skip the pull after a restore. The checkout is already the code to run, and another pull would land a second change in the middle of a migration.
# A tarball tree has no repository, so this refuses instead of letting git pull look like broken tooling. pull_blocked lets a dry-run refusal stop the preview; a real run has already died.
pull_blocked=0
if [[ "$FROM_RESTORE" == "0" ]]; then
    if [[ -d "$REPO_ROOT/.git" ]]; then
        branch="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD || true)"
        if [[ -z "$branch" ]]; then
            refuse "this checkout is on a DETACHED HEAD — 'git pull' has no branch to
  update. This framework's release branch is main: run 'git checkout main' to
  return to it, then re-run. (If you deliberately want a specific pinned tag
  rather than the moving branch, check that tag out instead — or use the
  tarball route and re-run with --from-restore semantics.)" || pull_blocked=1
        else
            UPDATE_BRANCH="$branch"
            _branch_notice

            # Refuse before git pull. A squash-merged branch is deleted on the remote while the local upstream still names it, and git's own error reads as broken tooling. Do not switch branches; that choice is the operator's.
            # No upstream is one failure. A deleted remote branch is another, and only ls-remote sees it, because the local tracking ref survives an unpruned fetch.
            upstream="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)"
            if [[ -z "$upstream" ]]; then
                refuse "branch '$branch' has no upstream configured — 'git pull' has nothing
  to pull FROM. Set one (git branch --set-upstream-to=origin/$branch $branch)
  or check out the branch you intend to run, then re-run.
  (Or use the tarball route and re-run with --from-restore semantics.)" || pull_blocked=1
            else
                # ls-remote exit 2 means the remote answered and the branch is gone. Any other nonzero status is transport failure, not a deleted branch, or every offline upgrade would be refused. timeout stops a hanging remote from stalling the run.
                timeout 10 git -C "$REPO_ROOT" ls-remote --exit-code --heads origin "$branch" \
                    >/dev/null 2>&1
                ls_rc=$?
                if [[ "$ls_rc" -eq 2 ]]; then
                    refuse "branch '$branch' no longer exists on origin — most likely its PR was
  merged (this repo squash-merges and deletes the branch on merge). 'git pull'
  cannot resolve an upstream that is gone, and would fail here with git's own
  raw ref error instead of telling you this. Check out the branch you
  actually intend to run — main, unless you have a specific reason not to —
  then re-run.
  (Or use the tarball route and re-run with --from-restore semantics.)" || pull_blocked=1
                elif [[ "$ls_rc" -ne 0 ]]; then
                    ylw "   ⚠ could not verify branch '$branch' still exists on origin (git
     ls-remote exited $ls_rc — likely offline, behind a proxy, or the remote
     is slow/unreachable, not a definitive answer). Proceeding without that
     check: 'git pull' will produce its own honest error if the branch
     really is gone."
                fi
            fi
        fi

        if [[ "$pull_blocked" == "0" ]]; then
            # Capture the pre-upgrade config from the old checkout, immediately before the pull (decision:1846). The equality only holds if this runs against that old copy. SM_PRE_UPDATE_VERSION is exported first because the capture stamps captured_by from it.
            SM_PRE_UPDATE_VERSION="$(sed -n 's/^FRAMEWORK_VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
                "$REPO_ROOT/shared-memory/scripts/coordinator.py" | head -1)"
            export SM_PRE_UPDATE_VERSION
            if [[ "$SKIP_ENV_MIGRATION" == "1" ]]; then
                step=$((step + 1))
                echo; ylw "── Step $step: capture pre-upgrade effective config"
                echo "   SKIPPED — --skip-env-migration given. §4 coverage for this"
                echo "   upgrade is deferred to a manual standalone run of migrate_env.py."
            else
                if [[ "$DRY_RUN" == "0" ]]; then
                    # Without -e, a failed mktemp would leave PREIMAGE_JSON pointed at /preimage.json. Refuse before the pull.
                    PREIMAGE_DIR="$(mktemp -d)" \
                        || die "could not create a temp directory for the pre-upgrade capture (mktemp failed) — refusing before the pull."
                    [[ -n "$PREIMAGE_DIR" && -d "$PREIMAGE_DIR" ]] \
                        || die "mktemp -d returned an unusable path — refusing before the pull."
                    chmod 700 "$PREIMAGE_DIR"
                    PREIMAGE_JSON="$PREIMAGE_DIR/preimage.json"
                    # Chain the cleanup onto the exit list instead of replacing it. The function reads PREIMAGE_DIR at call time, so the path is not interpolated into a trap string.
                    _cleanup_preimage_dir() { rm -rf -- "$PREIMAGE_DIR"; }
                    add_exit_cleanup _cleanup_preimage_dir
                else
                    PREIMAGE_JSON="\$(mktemp -d)/preimage.json"
                fi
                run_soft "capture pre-upgrade effective config" \
                    uv run --no-project --with-requirements "$REPO_ROOT/requirements-gateway.lock" \
                    python "$REPO_ROOT/shared-memory/scripts/migrate_env.py" \
                    --capture-preimage "$PREIMAGE_JSON"
                rc=$?
                if [[ "$DRY_RUN" == "0" && "$rc" != "0" ]]; then
                    die "capture pre-upgrade effective config FAILED (exit $rc) — refusing
  before the pull (nothing has moved; cost zero). SM_PRE_UPDATE_VERSION=
  $SM_PRE_UPDATE_VERSION. Re-run with --skip-env-migration to defer §4 coverage
  for this upgrade to a manual standalone run of migrate_env.py, or fix the
  failure above and re-run."
                fi
            fi

            run "fetch new code (branch: $branch)" git -C "$REPO_ROOT" pull --ff-only
        fi
    else
        refuse "no .git here — this host took the TARBALL route. Unpack the new
  tag's tarball beside this tree, carry shared-memory/.env across, and run this
  script from the NEW directory. There is nothing for 'git pull' to do." || pull_blocked=1
    fi
fi

# A dry run that predicted a refusal stops here. A real run would have died and never reached the later steps, so previewing them would be a lie.
if [[ "$pull_blocked" == "1" ]]; then
    echo
    red "✗ [DRY RUN] the real run stops here — step 0 would refuse (see above)."
    red "  Steps 1 onward are UNREACHABLE from this state and were not evaluated."
    exit 1
fi

# Back up through ops/backup.sh before any migration. A dump invented here would be a format restore.sh has never read, and retention can still prune this set by age.
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

# Migrate .env after the backup and before Postgres, on both the upgrade and restore paths, using this checkout's migrate_env.py. A captured pre-image is applied when step 0 made one; otherwise the current loader self-captures, which is a same-generation no-op unless the pre-image came from an older loader.
if [[ "$SKIP_ENV_MIGRATION" == "1" ]]; then
    step=$((step + 1))
    echo; ylw "── Step $step: migrate .env to explicit configuration"
    echo "   SKIPPED — --skip-env-migration given. §4 coverage for this upgrade"
    echo "   is deferred to a manual standalone run of migrate_env.py."
elif [[ -n "$PREIMAGE_JSON" ]]; then
    run_soft "migrate .env to explicit configuration" \
        uv run --no-project --with-requirements "$REPO_ROOT/requirements-gateway.lock" \
        python "$REPO_ROOT/shared-memory/scripts/migrate_env.py" \
        --apply --preimage "$PREIMAGE_JSON"
    rc=$?
    if [[ "$DRY_RUN" == "0" && "$rc" != "0" ]]; then
        die "migrate .env to explicit configuration FAILED (exit $rc, message above).
  Re-run with --skip-env-migration to defer §4 coverage for this upgrade to a
  manual standalone run of migrate_env.py, or fix the failure above and re-run —
  every earlier step (backup included) is idempotent."
    fi
else
    run_soft "migrate .env to explicit configuration (self-capture)" \
        uv run --no-project --with-requirements "$REPO_ROOT/requirements-gateway.lock" \
        python "$REPO_ROOT/shared-memory/scripts/migrate_env.py" \
        --apply
    rc=$?
    if [[ "$DRY_RUN" == "0" && "$rc" != "0" ]]; then
        die "migrate .env to explicit configuration FAILED (exit $rc, message above).
  Re-run with --skip-env-migration to defer §4 coverage for this upgrade to a
  manual standalone run of migrate_env.py, or fix the failure above and re-run —
  every earlier step (backup included) is idempotent."
    fi
fi

# apply.py resumes from the database's own ledger. Exit 3 means the database names migrations this checkout does not contain, which used to be reported as up to date.
run_soft "Postgres migrations (apply.py — forward-only)" \
    uv run --with psycopg2-binary python "$REPO_ROOT/shared-memory/migrations/apply.py"
rc=$?
if [[ "$DRY_RUN" == "0" && "$rc" == "3" ]]; then
    die "the database is AHEAD of this checkout (apply.py exit 3, message above).
  Nothing was migrated. Update the CHECKOUT to a release containing those
  migrations and re-run — the schema cannot be moved backwards."
fi
if [[ "$DRY_RUN" == "0" && "$rc" == "2" ]]; then
    # Exit 2 is a populated database with an empty ledger, the usual shape of a pre-v0.8.35 dump. This script must not adopt or re-run those migrations; both guesses destroy data, so the operator chooses.
    die "this database has the framework schema but NO migration ledger (apply.py
  exit 2, message above). Nothing was migrated.

  apply.py's own message above names both origins this reaches from — a backup
  taken before v0.8.35, or an install whose init_db.sh predates the automatic
  adoption step it now runs right after creating the schema. Either way those
  migrations HAVE been applied. Record that once, WITHOUT re-running them, then
  re-run this script:

      uv run --with psycopg2-binary python shared-memory/migrations/apply.py --adopt

  ⛔ Do not adopt a database you cannot vouch for. Adoption marks every migration
  present today as done; anything genuinely missing stays missing, silently."
fi
[[ "$DRY_RUN" == "0" && "$rc" != "0" ]] && die "apply.py failed (exit $rc) — stopping before the graph half."

# Neo4j has no ledger, so a constraint added later never reaches an old instance. A missing uniqueness constraint is silent: MERGE still works and only a race shows the duplicate. apply.py cannot reach Neo4j.
run_soft "Neo4j constraints (no ledger exists — verify every time)" \
    uv run --with neo4j python "$REPO_ROOT/shared-memory/migrations/verify_neo4j_init.py" --apply
rc=$?
[[ "$DRY_RUN" == "0" && "$rc" != "0" ]] && die "Neo4j constraint check FAILED (exit $rc) even with --apply.
  A declared constraint is not in force and could not be created — commonly a
  plain index blocking a uniqueness constraint. Stopping BEFORE the restart:
  a missing uniqueness constraint is silent, and MERGE keeps working."

# apply.py cannot stamp :Project nodes. Skipping this does not break writes; cross-project synthesis fails closed and looks like a corpus with nothing to fold.
run_soft "stamp project identity onto :Project nodes (graph half of migration 027)" \
    uv run --with psycopg2-binary --with neo4j python \
    "$REPO_ROOT/shared-memory/scripts/reconcile_project_identity.py" --apply
rc=$?
[[ "$DRY_RUN" == "0" && "$rc" != "0" ]] && die "project identity reconcile FAILED (exit $rc).
  Writes would still work, so this is easy to wave through — but cross-project
  synthesis fails CLOSED on unidentified nodes and presents as a quiet corpus
  rather than an error. Fix it here, where it is still visible."

# Every step after the restart assumes the running process is the migrated code. Refuse a missing systemctl before that restart, not from the middle of it.
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

    # A health answer is not proof the new code is running. An old process still listening would accept the repair rows below and blank record content, so the version must match this checkout.
    _running="$(curl -s --max-time 5 "$GATEWAY_URL/health" \
        | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    _expected="$(sed -n 's/^FRAMEWORK_VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$REPO_ROOT/shared-memory/scripts/coordinator.py" | head -1)"
    if [[ -z "$_running" || -z "$_expected" ]]; then
        die "could not read the gateway version (running='$_running',
  checkout='$_expected'). Refusing to continue: the next step enqueues rows that
  an older worker would use to blank record content."
    fi
    if [[ "$_running" != "$_expected" ]]; then
        die "the gateway answering $GATEWAY_URL is version $_running, but this
  checkout is $_expected — the restart did NOT replace the running process.
  The schema is migrated; the gateway is not. Refusing to continue, because the
  next step enqueues repair rows that an older worker turns into blanked records.
  Check:  systemctl --user status $GATEWAY_UNIT
          journalctl --user -u $GATEWAY_UNIT -n 50"
    fi
    grn "   ✓ gateway restarted and running $_running (matches this checkout)"
fi

# Report the config the restarted gateway is running (decision:1832). Before the restart it would describe the old process. Use the unit's locked uv invocation, and do not sit this between a run_soft and its rc capture.
# Dry-run must not execute it. Exit codes 0, 1, and 2 are the reporter's own results; anything past that is a crash and is printed.
echo
ylw "── Reporting effective configuration (check_config.py) ──"
if [[ "$DRY_RUN" == "0" ]]; then
    rc=0
    (cd "$REPO_ROOT" && uv run --no-project --with-requirements requirements-gateway.lock \
        python shared-memory/scripts/check_config.py) || rc=$?
    if [ "$rc" -gt 2 ]; then
        echo "⚠ check_config aborted (rc=$rc) — config report incomplete"
    fi
else
    echo "   (dry run — not executed)"
fi

# Run the domain backfill only after the restart. An older worker does not recognise the repair row, falls through to the fact branch, and blanks record content.
# The dry-run line includes --apply, because a real run writes. The step is opt-in (fact:1734); --no-domain-backfill is a no-op so an old invocation still parses, and the step number still advances.
if [[ "$NO_DOMAIN_BACKFILL_NOTICE" == "1" ]]; then
    echo
    ylw "── Notice: --no-domain-backfill"
    echo "   This flag is a no-op as of this release — the domain backfill is now"
    echo "   opt-in, so omitting every domain-backfill flag already skips it. Pass"
    echo "   --domain-backfill instead to run it. This flag and notice go away next"
    echo "   release."
fi
if [[ "$DOMAIN_BACKFILL" != "1" ]]; then
    step=$((step + 1))
    echo; ylw "── Step $step: domain backfill"
    echo "   SKIPPED — opt-in as of this release. Pass --domain-backfill to run it."
elif [[ "$DRY_RUN" == "1" ]]; then
    run "domain backfill — opt-in via --domain-backfill (writes domain rows via the outbox)" \
        uv run --with psycopg2-binary python \
        "$REPO_ROOT/shared-memory/scripts/backfill_domain_of.py" --apply
else
    run "domain backfill — apply (AFTER the restart; see the guard above)" \
        uv run --with psycopg2-binary python \
        "$REPO_ROOT/shared-memory/scripts/backfill_domain_of.py" --apply
fi

# Refresh skills after the restart. Before it, update_skill.sh compares the new client to the old gateway and reports an incompatibility that the restart is about to remove.
run_soft "refresh installed agent skills" bash "$REPO_ROOT/shared-memory/scripts/sync_skills.sh"
rc=$?
if [[ "$DRY_RUN" == "0" && "$rc" != "0" ]]; then
    # Not fatal: the gateway is already migrated. A stale skill copy fails silently, so the exit is still printed.
    ylw "   ! sync_skills.sh exited $rc — installed client skills may be STALE."
    ylw "     Re-run it by hand and check each agent's version before trusting them."
fi

# Read image-pin drift and never recreate containers here. reconcile_stack.sh is the operator's own step. A failure to run it is "unknown", not drift and not clean, and it must not fail the update.
STACK_DRIFT_VERDICT="unknown"    # unknown | none | drift
STACK_DRIFT_TABLE=""
if [[ -x "$REPO_ROOT/shared-memory/scripts/reconcile_stack.sh" ]]; then
    STACK_DRIFT_TABLE="$(bash "$REPO_ROOT/shared-memory/scripts/reconcile_stack.sh" --dry-run 2>&1)"
    _drift_rc=$?
    case "$_drift_rc" in
        0) STACK_DRIFT_VERDICT="none" ;;
        2) STACK_DRIFT_VERDICT="drift" ;;
    esac
fi

# Appended to every terminal line when drift exists, including a postflight failure, so the last line still says it.
_drift_suffix=""
[[ "$STACK_DRIFT_VERDICT" == "drift" ]] && _drift_suffix=" — stack reconcile REQUIRED"

# Print the drift table only when drift was found, so every terminal path can call this.
_stack_drift_notice() {
    [[ "$STACK_DRIFT_VERDICT" == "drift" ]] || return 0
    echo
    ylw "   ══════════════════════════════════════════════════════════════════"
    ylw "   STACK UPDATE REQUIRED — the shipped image pins moved and this"
    ylw "   update did not touch the containers by design."
    ylw "   ══════════════════════════════════════════════════════════════════"
    while IFS= read -r _drift_line; do
        printf '   %s\n' "$_drift_line"
    done <<< "$STACK_DRIFT_TABLE"
    ylw "   Run:    bash shared-memory/scripts/reconcile_stack.sh --dry-run"
    ylw "   Then:   bash shared-memory/scripts/reconcile_stack.sh"
    echo
}

# postflight exits 1 when AGENT_TOKEN is unset, because A1, A5, and A8 skip. That is the usual false failure of this step.
# Linger is reported inline, and only as a problem if the gateway is a systemd --user service. Another supervisor does not die with the session.
if [[ "$DRY_RUN" == "0" && -z "${AGENT_TOKEN:-}" ]]; then
    ylw "   ! AGENT_TOKEN is not exported — postflight's A1/A5/A8 will SKIP and it"
    ylw "     will exit 1. Export an agent token and run postflight yourself:"
    ylw "       AGENT_TOKEN=<token> bash shared-memory/scripts/postflight.sh"
    _branch_notice
    if [[ "$LINGER_VERDICT" == "no" ]]; then
        echo
        red "   ✗ Also: linger is NOT enabled for $_linger_who on this host. If the gateway"
        red "     runs as a systemd --user service, it will still be killed on session end"
        red "     even once postflight has been run. Fix:"
        echo "       sudo loginctl enable-linger $_linger_who"
    else
        _linger_brief
    fi
    _stack_drift_notice
    echo
    if [[ "$STACK_DRIFT_VERDICT" == "drift" ]]; then
        ylw "Update finished, but UNVERIFIED — stack reconcile REQUIRED. An update is not complete until postflight passes."
    else
        ylw "Update finished, but UNVERIFIED. An update is not complete until postflight passes."
    fi
    exit 1
fi
run_soft "postflight — verify end to end" bash "$REPO_ROOT/shared-memory/scripts/postflight.sh"
rc=$?

echo
if [[ "$DRY_RUN" == "1" ]]; then
    grn "Dry run complete — nothing was executed."
    _branch_notice
    if [[ "$LINGER_VERDICT" == "no" ]]; then
        red "  linger is NOT enabled for $_linger_who on this host. If the gateway runs as a"
        red "  systemd --user service, it will not survive your session ending. Fix:"
        echo "    sudo loginctl enable-linger $_linger_who"
    else
        _linger_brief
    fi
    _stack_drift_notice
elif [[ "$rc" == "0" ]]; then
    if [[ "$STACK_DRIFT_VERDICT" == "drift" ]]; then
        grn "Update complete and VERIFIED — postflight passed — stack reconcile REQUIRED."
    else
        grn "Update complete and VERIFIED — postflight passed."
    fi
    _branch_notice
    if [[ "$LINGER_VERDICT" == "no" ]]; then
        red "  BUT linger is NOT enabled for $_linger_who on this host. If the gateway runs"
        red "  as a systemd --user service, it will be killed when this session ends. Fix:"
        echo "    sudo loginctl enable-linger $_linger_who"
    else
        _linger_brief
    fi
    _stack_drift_notice
    ylw "Recommended: take a second backup now, so a known-good set exists at the new level."
else
    _branch_notice
    _stack_drift_notice
    if [[ "$LINGER_VERDICT" == "no" ]]; then
        die "postflight FAILED (exit $rc)${_drift_suffix}. The code and schema have moved; the system is
  NOT verified. Read the failures above before using this deployment.

  Also: linger is NOT enabled for $_linger_who on this host. If the gateway
  runs as a systemd --user service, it will still be killed on session end
  even once postflight passes. Fix:  sudo loginctl enable-linger $_linger_who"
    else
        _linger_brief
        die "postflight FAILED (exit $rc)${_drift_suffix}. The code and schema have moved; the system is
  NOT verified. Read the failures above before using this deployment."
    fi
fi
