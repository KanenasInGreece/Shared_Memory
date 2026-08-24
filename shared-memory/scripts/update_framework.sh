#!/usr/bin/env bash
#
# update_framework.sh — bring a Shared Memory deployment forward to the code
# in this checkout, and prove the result.
#
#   bash shared-memory/scripts/update_framework.sh              # upgrade in place
#   bash shared-memory/scripts/update_framework.sh --from-restore
#   bash shared-memory/scripts/update_framework.sh --dry-run    # print, run nothing
#   bash shared-memory/scripts/update_framework.sh --no-domain-backfill  # everything except step 6
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

# ── RULING 1: dry-run-aware refusal for the pre-`git pull` branch guard ─────
# --dry-run is documented as "print, run nothing" -- the way an operator
# finds out what WOULD happen. A refusal inside the guard below must never
# become the thing that makes a dry run itself fail. Under --dry-run, print
# the same message as an unmistakably PREDICTED outcome and return non-zero
# to the caller (which skips only the blocked step) instead of exiting; a
# real run still calls die() and stops exactly as it always has. Used ONLY
# by the branch guard in step 0 -- every other refusal in this script keeps
# calling die() directly.
refuse() {
    if [[ "$DRY_RUN" == "1" ]]; then
        red "✗ [DRY RUN — PREDICTED: a real run would refuse here] $*"
        return 1
    fi
    die "$*"
}

# ── RULING B: which branch this run pulled, made VISIBLE rather than silent ──
#
# Step 0's own "fetch new code (branch: $branch)" line names the branch, but
# it is one line early in a long run, easy to scroll past — and nothing
# repeats it at the end, where an operator actually checks whether the update
# succeeded. "Upgrade to main" never verified you were ON main: a host
# running a feature branch that still exists on the remote would pull it
# forward and exit 0, having upgraded nothing to the release, with no message
# anywhere saying so. This is measured, not refused outright — running a
# branch deliberately is legitimate — but the operator must not be able to
# mistake "pulled a stale branch" for "upgraded to the latest release".
# $UPDATE_BRANCH is set once, in step 0, and this notice is called again at
# every terminal path near the closing banner (same pattern as the linger
# verdict below), so it survives to the end regardless of which path the run
# takes.
UPDATE_BRANCH=""
_branch_notice() {
    [[ -n "$UPDATE_BRANCH" && "$UPDATE_BRANCH" != "main" ]] || return 0
    ylw "   ⚠ this checkout is on branch '$UPDATE_BRANCH', not main. Pulling it forward"
    ylw "     moves '$UPDATE_BRANCH' to ITS OWN latest commit — NOT to the latest release."
    ylw "     If you intend to upgrade to the released code, check out main and re-run."
}

FROM_RESTORE=0
DRY_RUN=0
SKIP_BACKUP=0
NO_DOMAIN_BACKFILL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-restore)       FROM_RESTORE=1; shift ;;
        --dry-run)            DRY_RUN=1; shift ;;
        --skip-backup)        SKIP_BACKUP=1; shift ;;
        --no-domain-backfill) NO_DOMAIN_BACKFILL=1; shift ;;
        -h|--help)      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
        *)              die "unknown argument: $1" ;;
    esac
done

step=0
# ⛔ run() DIES ON FAILURE. It used to return the exit code and leave checking to
# the caller, and three steps never checked — `git pull`, the gateway restart,
# and the domain backfill. Each failure then carried on:
#   * a failed pull migrated the OLD code while reporting success;
#   * a failed restart left the previous gateway answering, so the health wait
#     passed and the backfill enqueued rows against an OLD worker — the exact
#     content-blanking hazard this script's own comments warn about;
#   * a failed backfill was simply skipped past.
# Gating by default means a step added later is safe unless it opts out, which is
# the opposite of the trap above. Use run_soft() where the caller genuinely
# inspects the code (apply.py's exit 2/3) or tolerates failure (skill sync).
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

# ── Linger check (update path): READ the persistent flag, never enable it ────
#
# A host can report `systemctl --user is-active` = active while nothing
# listens on :8888, because without linger systemd tears down the user
# manager the moment the last session ends and takes the gateway with it —
# the next login starts it again, so a check run INSIDE a session can never
# observe the failure by itself. install_service.sh enables linger on first
# install and verifies it there; nothing re-checks an ALREADY-INSTALLED host
# — a host installed before this existed, or one whose linger flag was later
# flipped off by an admin or tool that doesn't know what depends on it, gets
# no warning. This function only READS the flag. Enabling it stays owned by
# install_service.sh — duplicating that logic here would give the two copies
# a chance to drift.
#
# Verdict is printed to stdout as exactly one of: yes | no | not-applicable.
#
# PRIMARY instrument: /var/lib/systemd/linger/<user>. systemd-logind creates
# this file the instant linger is enabled and removes it the instant it is
# disabled, so its existence IS the flag (verified on this host: world-
# readable, zero-byte, named for the user) — a plain existence test needs no
# privilege and, whenever the parent directory itself exists, cannot be
# misread. This replaces an earlier design that keyed the verdict on
# "did loginctl exit 0", which is wrong: `loginctl show-user <user>
# --property=Linger` exits 1 with "User ID N is not logged in or lingering"
# for a user with no session AND no linger — logind DID answer, definitively
# NO, and treating every nonzero exit as "logind didn't answer" turned that
# into a silent `not-applicable` on exactly the population this check exists
# for (a cron job, `systemd-run`, `sudo -u svc ...` — no session, no linger).
#
# SECONDARY / corroborating instrument: loginctl, consulted only when the
# linger directory itself does not exist (this host may simply never have
# had linger enabled for anyone, or may not run logind at all — the file
# test alone can't distinguish those). Its rc!=0 output is read literally:
# "is not logged in or lingering" is a definitive negative, not a failure to
# answer; anything else unrecognised (unknown user, no D-Bus, logind not
# running) is genuinely unanswered and reported as not-applicable. Bounded
# with `timeout` — a check documented as read-only and non-fatal must not be
# able to stall an upgrade on a wedged or absent D-Bus.
#
# ── RULING 3: the linger directory is a FUNCTION PARAMETER, never an
# environment read. An earlier version read "${LINGER_DIR:-...}" straight
# from the live environment, so `export LINGER_DIR=/tmp` silently bypassed
# the whole check on a REAL production run — a test seam reachable from
# outside the test suite is a backdoor, not a seam. The call site below
# (LINGER_VERDICT="$(check_linger)") passes no argument at all, so a
# production run always resolves the literal default; the environment now
# has zero influence over the verdict. Tests that need the file-presence
# branch without root pass the path explicitly as $1 instead.
#
# Self-contained: this function depends on NO script-level state (no colors,
# no run_soft, no DRY_RUN, no $step) so it can be extracted between the
# markers and run standalone — which is exactly what the test suite does.
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

    # rc != 0. logind can still have answered a DEFINITIVE negative: "User ID
    # N is not logged in or lingering" means linger is OFF, not that logind
    # failed to respond. Anything else (unknown user, no D-Bus, logind not
    # running) is genuinely unanswered.
    if echo "$out" | grep -q "is not logged in or lingering"; then
        echo "no"
    else
        echo "not-applicable"
    fi
}
# <<< LINGER_CHECK

# ── Linger verdict — measured HERE, in the preamble, before anything that
# can die (the missing-.env check right below, the missing-tool check, the
# migrations, the restart). Read-only and free, so measuring it costs
# nothing regardless of what happens next; the point is that whichever
# terminal path this run actually takes — the success banner, the dry-run
# banner, the AGENT_TOKEN early exit, or a postflight-failure `die` — it
# already has a verdict to report. This is NOT a numbered step: it changes
# no state, so it does not belong in the step count, and no later message
# may point at "Step N" for it.
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

# ── Preconditions, checked BEFORE anything is fetched or dumped ──────────────
#
# These are the cheapest and most certain checks in the whole script, and they
# used to run LAST — by failing at the first `uv run`, which is step 2. In
# upgrade mode that means a `git pull` and a FULL BACKUP had already happened
# before the run died on a missing binary. Cost paid, nothing achieved.
#
# ⚠ `uv` is the one that actually bites, and not because hosts lack it: the
# upstream installer puts it in ~/.local/bin, which a LOGIN shell resolves and a
# PROFILE-FREE shell does not. An agent driving this over ssh, or from a
# systemd unit, gets "uv: command not found" on a host where the operator can
# run uv perfectly well by hand. That is the DEFAULT outcome of a correct
# install, so it is named here rather than treated as a broken machine.
missing=""
for tool in uv curl; do
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
        # pull_blocked tracks a refuse() that fired under --dry-run (which
        # returns rather than exiting) so the block below knows to skip
        # 'git pull' without also skipping the rest of the dry run. On a
        # real run refuse() calls die() and this variable is never read.
        pull_blocked=0
        if [[ -z "$branch" ]]; then
            refuse "this checkout is on a DETACHED HEAD — 'git pull' has no branch to
  update. Check out the release branch or tag you intend to run, then re-run.
  (Or use the tarball route and re-run with --from-restore semantics.)" || pull_blocked=1
        else
            UPDATE_BRANCH="$branch"
            _branch_notice

            # ⛔ RULING A: refuse BEFORE 'git pull', the same voice and structure as
            # the detached-HEAD and tarball refusals above — rather than letting
            # 'git pull --ff-only' die with git's own raw
            #   "...but no such ref was fetched"
            # which reads as broken tooling, not as the (nameable, recoverable)
            # state it actually is. (Measured: a host whose checkout sat on a
            # MERGED feature branch hit exactly this — this repo squash-merges
            # and DELETES the branch when its PR merges, so the local upstream
            # config still names a ref that no longer exists on the remote, and
            # fetch returns nothing for it.)
            #
            # Two distinct bad states, two cheap read-only instruments:
            #   * never tracked at all  -> `rev-parse --abbrev-ref @{upstream}` fails
            #   * tracked, but deleted on the remote (the measured case) -> the local
            #     remote-tracking ref survives a plain fetch (nothing prunes it), so
            #     @{upstream} still resolves; `ls-remote --heads origin <branch>` is
            #     what actually detects the deletion, because it asks the remote
            #     directly instead of trusting a local ref that could be stale.
            #
            # ⛔ NEVER auto-switch branches here. Which branch to run is the
            # operator's decision, not the script's — refuse and explain, then let
            # them choose.
            upstream="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)"
            if [[ -z "$upstream" ]]; then
                refuse "branch '$branch' has no upstream configured — 'git pull' has nothing
  to pull FROM. Set one (git branch --set-upstream-to=origin/$branch $branch)
  or check out the branch you intend to run, then re-run.
  (Or use the tarball route and re-run with --from-restore semantics.)" || pull_blocked=1
            else
                # ── RULING 2: distinguish "the remote answered and the branch
                # is absent" from "the remote never answered" — the SAME
                # defect class the linger check above already exists to fix:
                # treating "no answer" as a definitive negative. `git
                # ls-remote --exit-code --heads` returns exactly 2 when the
                # remote was reached and found no matching ref (a DEFINITIVE
                # negative — refuse); any other non-zero code is a
                # transport/network failure (offline, a proxy, a slow or
                # unreachable remote) and must NOT be read as "branch
                # deleted" — that would false-refuse every offline upgrade.
                # Verified locally: exit 0 branch present, exit 2 remote
                # reached/branch absent, exit 128 unreachable remote (bad
                # path or bad host) — never 2. Bounded by `timeout`, the same
                # pattern as the linger check's `timeout 5 loginctl`, so a
                # hanging remote cannot stall an upgrade OR a dry run.
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
            run "fetch new code (branch: $branch)" git -C "$REPO_ROOT" pull --ff-only
        fi
    else
        refuse "no .git here — this host took the TARBALL route. Unpack the new
  tag's tarball beside this tree, carry shared-memory/.env across, and run this
  script from the NEW directory. There is nothing for 'git pull' to do." || true
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
run_soft "Postgres migrations (apply.py — forward-only)" \
    uv run --with psycopg2-binary python "$REPO_ROOT/shared-memory/migrations/apply.py"
rc=$?
if [[ "$DRY_RUN" == "0" && "$rc" == "3" ]]; then
    die "the database is AHEAD of this checkout (apply.py exit 3, message above).
  Nothing was migrated. Update the CHECKOUT to a release containing those
  migrations and re-run — the schema cannot be moved backwards."
fi
if [[ "$DRY_RUN" == "0" && "$rc" == "2" ]]; then
    # A populated database with an EMPTY ledger. Restoring an OLD dump is the
    # most likely way to arrive here, not the rarest: the ledger itself only
    # arrived in v0.8.35, so every backup taken before that carries the full
    # framework schema and no record of how it got there.
    #
    # ⛔ THIS SCRIPT MUST NOT DECIDE. apply.py refuses precisely because the two
    # available guesses are both destructive: adopting silently would skip a
    # genuinely new migration forever, and running them all would re-execute
    # migrations against a schema they were never written for — one of which
    # deletes rows on a key a later migration changed. The operator chooses,
    # once, having looked.
    die "this database has the framework schema but NO migration ledger (apply.py
  exit 2, message above). Nothing was migrated.

  If it came from a backup taken before v0.8.35, that is expected — the ledger
  did not exist yet, and those migrations HAVE been applied. Record that once,
  WITHOUT re-running them, then re-run this script:

      uv run --with psycopg2-binary python shared-memory/migrations/apply.py --adopt

  ⛔ Do not adopt a database you cannot vouch for. Adoption marks every migration
  present today as done; anything genuinely missing stays missing, silently."
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
run_soft "Neo4j constraints (no ledger exists — verify every time)" \
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
run_soft "stamp project identity onto :Project nodes (graph half of migration 027)" \
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

    # ⛔ "SOMETHING ANSWERS" IS NOT "THE NEW CODE IS RUNNING". If the restart
    # command fails — a typo in GATEWAY_RESTART_CMD, a unit that refuses to stop,
    # a supervisor that silently keeps the old process — the PREVIOUS gateway is
    # still listening and answers this check happily. Every later step then runs
    # against it, and step 6 enqueues repair rows only a current worker
    # understands: an older one falls through to its ordinary fact branch and
    # BLANKS THE CONTENT of every record it touches. So compare versions, which
    # is the one thing an old process cannot fake.
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
# ⚠ The preview used to run here and --apply on the very next line, with no
# pause between them: an operator could read what WOULD be enqueued only after
# it already had been. A preview nobody can act on is decoration, so the preview
# now belongs to --dry-run (where it is the whole point) and a real run applies
# once. The applied run reports what it did, which is the record that matters.
#
# ⛔ --no-domain-backfill declines the migration for THIS run only — default
# behaviour is unchanged, and every step after this one keeps its number
# (the skip idiom below still increments `step`; it just never calls run()).
if [[ "$NO_DOMAIN_BACKFILL" == "1" ]]; then
    step=$((step + 1))
    echo; ylw "── Step $step: domain backfill"
    echo "   SKIPPED — --no-domain-backfill given. No repair rows were enqueued;"
    echo "   re-run without the flag when you are ready to apply it."
elif [[ "$DRY_RUN" == "1" ]]; then
    run "domain backfill — preview (enqueues nothing)" \
        uv run --with psycopg2-binary python \
        "$REPO_ROOT/shared-memory/scripts/backfill_domain_of.py"
else
    run "domain backfill — apply (AFTER the restart; see the guard above)" \
        uv run --with psycopg2-binary python \
        "$REPO_ROOT/shared-memory/scripts/backfill_domain_of.py" --apply
fi

# ── Step 7: refresh the installed client skills ──────────────────────────────
#
# ⚠ AFTER the restart, deliberately. Run before it, update_skill.sh compares the
# new client against the OLD gateway and prints "Updated to X but still
# incompatible. The GATEWAY itself ..." — alarming, self-resolving one step
# later, and observed on two hosts. Ordering removes the false alarm rather than
# rewording it.
run_soft "refresh installed agent skills" bash "$REPO_ROOT/shared-memory/scripts/sync_skills.sh"
rc=$?
if [[ "$DRY_RUN" == "0" && "$rc" != "0" ]]; then
    # Not fatal: the gateway is already migrated and correct. But never silent —
    # skills are shipped as COPIES, and a stale copy fails silently forever.
    ylw "   ! sync_skills.sh exited $rc — installed client skills may be STALE."
    ylw "     Re-run it by hand and check each agent's version before trusting them."
fi

# ── Step 8: prove it ──────────────────────────────────────────────────────────
#
# ⚠ postflight NEEDS AGENT_TOKEN EXPORTED or A1/A5/A8 skip and it exits 1. That
# is documented behaviour, not a defect — but it is also the single most common
# way this step "fails" for a reason that has nothing to do with the update.
#
# The linger verdict measured in the preamble is reported inline at every
# terminal path below rather than pointing at a step number — it is not a
# step, so there is no "Step N" for a message to point at. The claim is
# phrased CONDITIONALLY ("if the gateway runs as a systemd --user service")
# because linger only matters to that deployment shape; this script also
# supports a gateway run under a different init, a container, or a bare
# process (see the GATEWAY_RESTART_CMD comment near the top), and on those a
# `no` verdict is real but describes nothing this operator's own session
# controls — asserting the kill as fact would be false on that host.
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
    fi
    echo
    ylw "Update finished, but UNVERIFIED. An update is not complete until postflight passes."
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
    fi
elif [[ "$rc" == "0" ]]; then
    grn "Update complete and VERIFIED — postflight passed."
    _branch_notice
    if [[ "$LINGER_VERDICT" == "no" ]]; then
        red "  BUT linger is NOT enabled for $_linger_who on this host. If the gateway runs"
        red "  as a systemd --user service, it will be killed when this session ends. Fix:"
        echo "    sudo loginctl enable-linger $_linger_who"
    fi
    ylw "Recommended: take a second backup now, so a known-good set exists at the new level."
else
    _branch_notice
    if [[ "$LINGER_VERDICT" == "no" ]]; then
        die "postflight FAILED (exit $rc). The code and schema have moved; the system is
  NOT verified. Read the failures above before using this deployment.

  Also: linger is NOT enabled for $_linger_who on this host. If the gateway
  runs as a systemd --user service, it will still be killed on session end
  even once postflight passes. Fix:  sudo loginctl enable-linger $_linger_who"
    else
        die "postflight FAILED (exit $rc). The code and schema have moved; the system is
  NOT verified. Read the failures above before using this deployment."
    fi
fi
