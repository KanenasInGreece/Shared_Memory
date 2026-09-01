#!/usr/bin/env bash
#
# uninstall_framework.sh — remove a Shared Memory installation from this host.
#
#   bash shared-memory/scripts/uninstall_framework.sh --level service --dry-run
#   bash shared-memory/scripts/uninstall_framework.sh --level data
#   bash shared-memory/scripts/uninstall_framework.sh --level all --yes
#
# LEVELS (operator-ruled: tiered, and --level is REQUIRED — there is no default,
# because the safe default for an irreversible operation is "say what you mean"):
#
#   service  the gateway stops being a service, and no agent can reach it.
#            systemd user unit, linger, and EVERY agent's skill directory
#            (which is where its raw AGENT_TOKEN lives). Containers, data and
#            .env are untouched — this level is reversible; the exact working
#            procedure (install_service.sh, sync_skills.sh --install, then a
#            per-agent bootstrap_tokens.sh --remint ... --install-path, then a
#            gateway restart) is printed at the end of a real run of this
#            level, not duplicated here where it could drift out of sync.
#
#   data     everything above, plus the stores and the credentials: containers
#            and volumes, the Neo4j/Postgres data directories, and
#            shared-memory/.env.
#            ⛔ NOT reversible. The corpus is gone unless a backup set exists.
#
#   all      everything above, plus LLM_MODELS_DIR (GGUF weights). A level named
#            "all" that quietly preserved 1.2 GB of models would not be "all".
#
# ⛔ WHAT IS NEVER REMOVED, AT ANY LEVEL:
#
#   * ~/.shared-memory — THE HOST'S RECORD, not the installation's. It holds the
#     backup sets, the credential audit trail, the capacity measurement history
#     and every postflight baseline. An audit trail an uninstall can erase is not
#     an audit trail; the measurements describe the machine, not the install.
#     Deleting a corpus and its backups in one command is unrecoverable, and the
#     backup is the ONLY preservation mechanism
#     this framework has: the stores bake credentials into their data directories
#     at init, so a preserved data dir without its .env is permanently unreadable.
#     That is also why there is no "keep my data" option — keeping data is what
#     the backup is for.
#
#   * THE REPOSITORY CHECKOUT. This script lives inside it and cannot delete the
#     ground it stands on cleanly. The final `rm -rf` is printed for the operator
#     to run deliberately.
#
# Exit 0 when the requested level is fully removed; non-zero on any refusal.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Same two candidates, same order, as every other loader in this project
# (apply.py's _load_env(), bootstrap_tokens.sh, ...): shared-memory/.env,
# falling back to the pre-0.6 repo-root path. Kept as an ARRAY too (not just
# the resolved $ENV_FILE below) because the mintlock cleanup further down
# must check both candidates regardless of which one currently exists — once
# $ENV_FILE itself is gone (a re-run after a partial uninstall), resolution
# below silently shifts to the OTHER candidate, and a mintlock stranded at
# the first one would never be found again if only $ENV_FILE were checked.
_ENV_CANDIDATES=("$REPO_ROOT/shared-memory/.env" "$REPO_ROOT/.env")
ENV_FILE="${_ENV_CANDIDATES[0]}"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${_ENV_CANDIDATES[1]}"

GATEWAY_UNIT="${GATEWAY_UNIT:-hive-mind-gateway.service}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/shared-memory/ops/postgres_neo4j_limits.yaml}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

LEVEL=""; DRY_RUN=0; ASSUME_YES=0; NO_BACKUP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --level)     LEVEL="${2:?--level needs service|data|all}"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        --no-backup) NO_BACKUP=1; shift ;;
        -h|--help)   awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
        *)           die "unknown argument: $1" ;;
    esac
done

case "$LEVEL" in
    service|data|all) ;;
    "")  die "--level is required (service | data | all). There is no default:
  this operation is irreversible from 'data' upward, so the level is something
  you state, never something you inherit." ;;
    *)   die "unknown level '$LEVEL' (expected service, data or all)" ;;
esac

# ── Read the installation's own description of itself ────────────────────────
# Same hand-rolled parse as every other loader in this project — never import a
# parser, which is how two verifiers came to report a credentials error for a
# missing dependency.
env_get() {
    local key="$1" line
    [[ -f "$ENV_FILE" ]] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -1)" || true
    [[ -n "$line" ]] && printf '%s' "${line#*=}" | tr -d '"'"'"''
}

NEO4J_HOST_DIR="$(env_get NEO4J_HOST_DIR)"
PG_DATA_DIR="$(env_get PG_DATA_DIR)"
LLM_MODELS_DIR="$(env_get LLM_MODELS_DIR)"
BACKUP_DIR="$(env_get BACKUP_DIR)"; BACKUP_DIR="${BACKUP_DIR:-$HOME/.shared-memory/backups}"
STATE_DIR="$HOME/.shared-memory"
UNIT_PATH="$HOME/.config/systemd/user/$GATEWAY_UNIT"

# Agent install directories: the REGISTRY first, because an install path is owned
# information about this host and not something a naming convention reproduces.
# The historical four are added only when they actually exist on disk.
#
# ⚠ AGENT_INSTALLS entries have TWO arities — `name:path` (kind `skill`, the
# permanent meaning of the two-field form) and `name:kind:path`. Stripping only
# the name and calling dirname on the rest turned `opencode:mcp:/w/.env` into
# the literal directory `mcp:`, so an MCP install's walled directory — which
# holds that agent's raw token, the very thing this inventory warns about —
# was neither listed nor removed, and a nonsense path was listed instead.
mapfile -t _registry_entries < <(
    grep -E '^AGENT_INSTALLS=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
    | tr ',' '\n' | sed -n 's/^[[:space:]]*[^:]*:[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/\1/p')
_registry_dirs=(); _registry_kinds=()
for _e in ${_registry_entries[@]+"${_registry_entries[@]}"}; do
    _k="skill"
    case "$_e" in
        skill:*) _e="${_e#skill:}" ;;
        mcp:*)   _k="mcp"; _e="${_e#mcp:}" ;;
    esac
    [[ -n "$_e" ]] || continue
    _registry_dirs+=("$(dirname "$_e")"); _registry_kinds+=("$_k")
done
SKILL_DIRS=(); SKILL_KINDS=()
_i=0
for d in ${_registry_dirs[@]+"${_registry_dirs[@]}"}; do
    [[ -n "$d" && -d "$d" ]] && { SKILL_DIRS+=("$d"); SKILL_KINDS+=("${_registry_kinds[$_i]}"); }
    _i=$((_i + 1))
done
for d in "$HOME/.claude/skills/shared-memory" "$HOME/.codex/skills/shared-memory" \
         "$HOME/.gemini/skills/shared-memory" "$HOME/.grok/skills/shared-memory"; do
    [[ -d "$d" ]] || continue
    _dup=0; for s in ${SKILL_DIRS[@]+"${SKILL_DIRS[@]}"}; do [[ "$s" == "$d" ]] && _dup=1; done
    [[ "$_dup" == "0" ]] && { SKILL_DIRS+=("$d"); SKILL_KINDS+=("skill"); }
done

_size() { [[ -e "$1" ]] && du -sh "$1" 2>/dev/null | awk '{print $1}' || echo "-"; }

# ── Inventory FIRST. Nothing is removed before the operator has seen this. ────
echo "Shared Memory — UNINSTALL (level: $LEVEL)"
echo "  repo   : $REPO_ROOT"
echo "  env    : ${ENV_FILE}$([[ -f "$ENV_FILE" ]] || echo ' (absent)')"
echo
echo "WILL BE REMOVED:"
[[ -f "$UNIT_PATH" ]] && echo "  systemd unit      $UNIT_PATH" || echo "  systemd unit      (not installed)"
echo "  systemd linger    disabled for $USER (re-enable with loginctl enable-linger)"
if [[ ${#SKILL_DIRS[@]} -eq 0 ]]; then
    echo "  agent skills      (none found)"
else
    _i=0
    for d in "${SKILL_DIRS[@]}"; do
        if [[ "${SKILL_KINDS[$_i]:-skill}" == "mcp" ]]; then
            echo "  MCP connector dir $d   ($(_size "$d"))  ⚠ contains that agent's raw token"
        else
            echo "  agent skill dir   $d   ($(_size "$d"))  ⚠ contains that agent's raw token"
        fi
        _i=$((_i + 1))
    done
fi
if [[ "$LEVEL" == "data" || "$LEVEL" == "all" ]]; then
    echo "  containers        docker compose -f $(basename "$COMPOSE_FILE") down -v  (volumes included)"
    [[ -n "$NEO4J_HOST_DIR" ]] && echo "  neo4j data        $NEO4J_HOST_DIR   ($(_size "$NEO4J_HOST_DIR"))"
    [[ -n "$PG_DATA_DIR"    ]] && echo "  postgres data     $PG_DATA_DIR   ($(_size "$PG_DATA_DIR"))"
    echo "                    ⚠ bind mounts owned by the containers' uid — 'compose down -v'"
    echo "                      does NOT remove them, and this user usually cannot either;"
    echo "                      a throwaway root container does it, sudo is the fallback."
    echo "  gateway env       $ENV_FILE   ⚠ holds every credential this install has"
    for _cand in "${_ENV_CANDIDATES[@]}"; do
        [[ -f "${_cand}.mintlock" ]] && echo "  mint lock         ${_cand}.mintlock"
    done
    echo "  (host state at $STATE_DIR is KEPT — see below)"
fi
if [[ "$LEVEL" == "all" ]]; then
    if [[ -n "$LLM_MODELS_DIR" && -d "$LLM_MODELS_DIR" ]]; then
        echo "  LLM models        $LLM_MODELS_DIR   ($(_size "$LLM_MODELS_DIR"))  ⚠ re-download"
    else
        echo "  LLM models        (LLM_MODELS_DIR unset or absent — nothing to remove)"
    fi
fi
echo
echo "WILL BE KEPT:"
echo "  host state        $STATE_DIR   ($(_size "$STATE_DIR"))"
echo "                    backups, the credential AUDIT TRAIL, capacity history and"
echo "                    postflight baselines. ⛔ An audit trail must outlive the"
echo "                    thing it audits, and the measurement history describes the"
echo "                    HOST, not the installation — reinstalling does not make"
echo "                    last month's numbers untrue."
echo "  backups           $BACKUP_DIR   ($(_size "$BACKUP_DIR"))"
echo "                    ⛔ never removed at any level — it is the only way back."
echo "  repo checkout     $REPO_ROOT"
echo "                    this script runs from inside it; the final rm -rf is yours."
if [[ "$LEVEL" == "service" ]]; then
    # M7 (decision:1824 item 3): --level service leaves shared-memory/.env in
    # place untouched (only data/all remove it, already listed above under
    # WILL BE REMOVED) — one plain line stating that plainly, so an operator
    # is never left to discover it: every declared LLM backend, its
    # private_ok/roles/extra_body state and any token_env NAME reference
    # survives, and a later restart resumes exactly the pool this file
    # declares.
    echo "  framework env     $ENV_FILE   (left in place — every declared LLM backend,"
    echo "                    its private_ok/roles/extra_body state and any token_env"
    echo "                    NAME reference survives untouched; a later restart resumes"
    echo "                    exactly the pool this file declares)"
fi
echo

# ── The backup gate. Irreversible levels refuse to start unencumbered. ───────
if [[ "$LEVEL" != "service" && "$NO_BACKUP" != "1" ]]; then
    _sets="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.manifest.json' 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "${_sets:-0}" -eq 0 ]]; then
        die "no backup set found in $BACKUP_DIR, and level '$LEVEL' destroys the
  stores. Take one first:

      bash shared-memory/ops/backup.sh

  Then re-run. If this install is genuinely disposable — a test host, a corpus
  you do not want — say so explicitly with --no-backup."
    fi
    _newest="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.manifest.json' 2>/dev/null | sort | tail -1)"
    grn "  ✓ backup gate: $_sets set(s) present, newest $(basename "${_newest%.manifest.json}")"
    ylw "  ! not verified here — run 'bash shared-memory/ops/backup.sh --verify' if unsure."
fi

# A dry run reports the gate's verdict too. "What would happen" includes "it
# would refuse" — a preview that hides the refusal is not a preview.
if [[ "$DRY_RUN" == "1" ]]; then
    grn "Dry run — nothing was removed."
    exit 0
fi

if [[ "$ASSUME_YES" != "1" ]]; then
    echo
    read -r -p "Type the level ('$LEVEL') to confirm: " _answer
    [[ "$_answer" == "$LEVEL" ]] || die "not confirmed — nothing was removed."
fi

echo
# ── service ──────────────────────────────────────────────────────────────────
echo "Removing the service ..."
# ⛔ `systemctl --user` TALKS TO THE SESSION BUS, NOT TO $HOME. Pointing HOME at
# a sandbox does NOT sandbox it: the calls below reach the real user's systemd
# instance whatever HOME says. Measured the hard way — a test running this
# script under a temporary HOME stopped and disabled the live gateway on the
# development machine, and disabled linger with it.
#
# So the service is touched only when THIS install's unit file is actually
# present. That is also the correct rule on its own terms: an installation that
# never installed a service has no service to remove, and stopping a unit this
# tree did not install is reaching outside the thing being uninstalled.
if [[ ! -f "$UNIT_PATH" ]]; then
    ylw "  ! no unit at $UNIT_PATH — this install has no service; leaving systemd alone"
elif command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop    "$GATEWAY_UNIT" 2>/dev/null || true
    systemctl --user disable "$GATEWAY_UNIT" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload 2>/dev/null || true
    grn "  ✓ unit stopped, disabled and removed"
    # Mirrors install_service.sh, which enables linger the same way and needs the
    # same sudo fallback: plain loginctl returns "Access denied" for a non-root
    # user on some distributions, and a silent failure here leaves the machine
    # in a state the install would not have produced.
    if loginctl disable-linger "$USER" >/dev/null 2>&1; then
        grn "  ✓ linger disabled"
    elif sudo -n loginctl disable-linger "$USER" >/dev/null 2>&1; then
        grn "  ✓ linger disabled (via sudo -n, as install enables it)"
    else
        ylw "  ! could not disable linger — run: sudo loginctl disable-linger $USER"
    fi
else
    ylw "  ! systemctl not found — nothing to stop"
fi

echo "Removing agent skill directories (each holds that agent's raw token) ..."
if [[ ${#SKILL_DIRS[@]} -eq 0 ]]; then
    echo "  (none)"
else
    for d in "${SKILL_DIRS[@]}"; do rm -rf "$d" && echo "  ✓ removed $d"; done
fi

[[ "$LEVEL" == "service" ]] && {
    echo
    grn "Uninstall complete (level: service)."
    echo "The stores, credentials and corpus are untouched. $ENV_FILE and every"
    echo "declaration in it (LLM_BACKENDS_JSON, private_ok/roles, AGENT_TOKENS, ...)"
    echo "stay exactly as they are — nothing else config-shaped changes at this level."
    echo "Reverse with:"
    echo "  1. bash shared-memory/ops/install_service.sh"
    echo "     (recreates the systemd unit + linger, starts the gateway)"
    echo "  2. bash shared-memory/scripts/sync_skills.sh --install"
    echo "     (plain sync_skills.sh SKIPS a deleted directory — it only updates an"
    echo "      EXISTING install. --install recreates + populates each directory named"
    echo "      in AGENT_INSTALLS= in $ENV_FILE — that registry entry survives this"
    echo "      uninstall level, so this step needs no agent name.)"
    echo "  3. Per agent (name + install path: see AGENT_INSTALLS= in $ENV_FILE):"
    echo "       bash shared-memory/scripts/bootstrap_tokens.sh --remint <name> --install-path <dir>/.env"
    echo "     (--remint with no --install-path mints a token nobody receives — always"
    echo "      pass it. Requires step 2 to have already created <dir>.)"
    echo "     For an entry written name:mcp:path — an MCP connector install — add --mcp"
    echo "     so it re-registers as one; without it the entry reverts to a CLI skill"
    echo "     install and the next sync delivers the wrong package there."
    echo "  4. systemctl --user restart $GATEWAY_UNIT"
    echo "     (the gateway from step 1 is still running the OLD AGENT_TOKENS digests;"
    echo "      this loads what step 3 wrote — bootstrap_tokens.sh says so itself.)"
    exit 0
}

# ── data ─────────────────────────────────────────────────────────────────────
# >>> COMPOSE_DOWN_AND_VERIFY
# The compose file requires NEO4J_HOST_DIR / PG_DATA_DIR to interpolate at all
# (postgres_neo4j_limits.yaml's `${VAR:?set ... in shared-memory/.env}` guards)
# — EVERY `docker compose` invocation against it, `down` included, needs those
# values or config parsing fails before docker touches a single container.
# Measured (fact:1515): invoking `down -v` without `--env-file` failed exactly
# that way, and the caller's `2>&1 | tail -3` discarded the exit code and
# printed success regardless — four `restart: always` containers kept running
# while the next block below deleted their data directories out from under
# them.
#
# So: (1) always pass --env-file when $ENV_FILE exists, the same shape the
# install side already uses (install_framework.sh / preflight.sh / AGENTS.md
# all print `-f ... --env-file ...`). (2) When it does not — a re-run after a
# partial uninstall already removed it — fall back to explicit dummy values
# for ONLY the two required-but-unused-by-`down` keys, just enough to satisfy
# compose's interpolation gate. This is an honest fallback, not a workaround:
# `down -v` never mounts or reads those paths, and this compose file declares
# no top-level `volumes:` at all — every volume in it is a bind mount, so `-v`
# has nothing of its own to remove either way (the real data dirs are handled
# by remove_data_dir() below, from the .env's actual values, while they still
# exist). The dummy values exist only so compose's config parser lets `down`
# run at all when the file that would have supplied real ones is gone.
# (3) CHECK the exit code — never swallow it behind a pipe again. (4) then
# MEASURE that the containers are actually gone (`docker ps -a`, matched
# against the compose file's own `container_name:` list) before saying so —
# compose exiting 0 is not proof by itself.
compose_down_and_verify() {
    local down_out down_rc containers name leftover=()

    if [[ -f "$ENV_FILE" ]]; then
        down_out="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down -v 2>&1)"
        down_rc=$?
    else
        ylw "  ! $ENV_FILE not found — env-less down. This can still stop and remove"
        ylw "    this stack's containers (addressed by their fixed container_name:,"
        ylw "    not by the missing values) but has no way to know the real bind-mount"
        ylw "    paths; that costs nothing here — 'down -v' only ever removes named"
        ylw "    Docker volumes, and this stack declares none, so it was never how the"
        ylw "    data directories got removed."
        down_out="$(NEO4J_HOST_DIR="${NEO4J_HOST_DIR:-/uninstall-env-file-missing}" \
                    PG_DATA_DIR="${PG_DATA_DIR:-/uninstall-env-file-missing}" \
                    docker compose -f "$COMPOSE_FILE" down -v 2>&1)"
        down_rc=$?
    fi

    if [[ "$down_rc" -ne 0 ]]; then
        red "  ✗ docker compose down FAILED (exit $down_rc) — nothing below this ran:"
        printf '%s\n' "$down_out" | tail -10 | sed 's/^/    /'
        return 1
    fi

    mapfile -t containers < <(grep -E '^[[:space:]]*container_name:' "$COMPOSE_FILE" \
        | sed -E 's/^[[:space:]]*container_name:[[:space:]]*//')

    # Ops & Release Integrity review, Critical (Ops-14), merger-verified.
    # FAILS OPEN otherwise: an empty $containers array (compose file syntax
    # changed, renamed, or missing entirely by the time this runs) makes the
    # loop below iterate zero times, find zero leftovers, and claim VERIFIED
    # success -- the exact unearned checkmark this whole function exists to
    # remove, reintroduced one layer up in its own verification step. An
    # empty parse is refused as a verification FAILURE, not treated as "zero
    # containers to check". No fallback heuristic (guessing container names)
    # -- an honest "cannot verify" beats a clever guess.
    if [[ ${#containers[@]} -eq 0 ]]; then
        red "  ✗ could not parse any container_name: entries from $COMPOSE_FILE --"
        red "    the teardown CANNOT be verified. This does not mean nothing is"
        red "    running: it means this function has no list to check docker ps"
        red "    against. Check by hand:"
        red "        docker ps -a"
        return 1
    fi

    local present
    present="$(docker ps -a --format '{{.Names}}' 2>/dev/null)"
    for name in "${containers[@]}"; do
        grep -qxF -- "$name" <<<"$present" && leftover+=("$name")
    done
    if [[ ${#leftover[@]} -gt 0 ]]; then
        red "  ✗ compose exited 0 but ${#leftover[@]} container(s) are STILL PRESENT:"
        for name in "${leftover[@]}"; do red "      $name"; done
        return 1
    fi

    grn "  ✓ compose stack down, verified gone (docker ps -a), volumes removed"
    return 0
}
# <<< COMPOSE_DOWN_AND_VERIFY

echo "Removing containers and volumes ..."
if command -v docker >/dev/null 2>&1 && [[ -f "$COMPOSE_FILE" ]]; then
    if ! compose_down_and_verify; then
        echo
        red "Uninstall INCOMPLETE (level: $LEVEL) — the compose stack could not be"
        red "brought down and verified gone (see above). Data directories and"
        red "$ENV_FILE were NOT touched — deleting them while containers may still be"
        red "running is exactly the failure this check exists to prevent."
        red "Fix the problem above and re-run."
        exit 1
    fi
else
    ylw "  ! docker or compose file absent — skipping ($COMPOSE_FILE)"
fi

# ⛔ THE STORES' DATA DIRECTORIES ARE NOT OWNED BY THIS USER.
# Measured on a live install: PG_DATA_DIR is mode 700 owned by the uid the
# Postgres image runs as, so the operator cannot even `ls` it, let alone remove
# it — `rm -rf` fails with EACCES. And because these are BIND MOUNTS rather than
# named volumes, `docker compose down -v` does not touch them either: without
# this step the corpus survives an uninstall that reported success.
#
# The fix is the idiom this project already uses to reach Postgres tooling —
# do the work where the permissions live, in a container — rather than asking
# the operator for sudo. Failure is reported, never swallowed: an earlier draft
# wrote `rm -rf "$d" && echo ✓`, which printed nothing at all when the remove
# failed and left the data behind under a success message.
remove_data_dir() {
    local d="$1"
    [[ -n "$d" && -e "$d" ]] || return 0
    if rm -rf "$d" 2>/dev/null && [[ ! -e "$d" ]]; then
        echo "  ✓ removed data dir $d"
        return 0
    fi
    if command -v docker >/dev/null 2>&1; then
        local parent base
        parent="$(dirname "$d")"; base="$(basename "$d")"
        docker run --rm -v "$parent:/_target" alpine:latest \
            rm -rf "/_target/$base" >/dev/null 2>&1 || true
        if [[ ! -e "$d" ]]; then
            echo "  ✓ removed data dir $d (as root, via a throwaway container)"
            return 0
        fi
    fi
    red "  ✗ COULD NOT REMOVE $d — it is owned by the container's uid and this"
    red "    user cannot delete it. The corpus is STILL ON DISK. Remove it with:"
    red "        sudo rm -rf $d"
    return 1
}
_data_failures=0
for d in "$NEO4J_HOST_DIR" "$PG_DATA_DIR"; do
    remove_data_dir "$d" || _data_failures=$((_data_failures + 1))
done

# ⛔ $STATE_DIR IS NOT TOUCHED (operator-ruled). It is not installation state —
# it is the HOST's record: the backup sets, the credential audit trail, the
# capacity measurement history, and every postflight baseline. An audit trail
# that an uninstall can erase is not an audit trail, and the measurements
# describe this machine rather than this installation, so reinstalling does not
# make them untrue. An earlier draft cleared it entry-by-entry keeping only
# backups; that would have destroyed the audit log and the capacity history for
# no benefit — and capacity history has already been lost once on this project.

[[ -f "$ENV_FILE" ]] && rm -f "$ENV_FILE" && echo "  ✓ removed $ENV_FILE (every credential this install had)"

# bootstrap_tokens.sh's read-modify-write lock on $ENV_FILE (see its own
# comment at _LOCKFILE=). It has no purpose once the .env it guards is gone,
# and — unlike $ENV_FILE above — nothing else in this script incidentally
# removed it, so a prior --level data run left it stranded on disk forever.
#
# Checked against BOTH candidates, not just the resolved $ENV_FILE: once
# .env is gone, resolution above silently shifts to the OTHER candidate, so
# a re-run after a partial uninstall (.env already gone, mintlock still
# sitting where that .env used to be) would otherwise look in the wrong
# place and never find it.
for _cand in "${_ENV_CANDIDATES[@]}"; do
    [[ -f "${_cand}.mintlock" ]] && rm -f "${_cand}.mintlock" && echo "  ✓ removed ${_cand}.mintlock (bootstrap_tokens.sh's mint lock)"
done

# ── all ──────────────────────────────────────────────────────────────────────
if [[ "$LEVEL" == "all" ]]; then
    if [[ -n "$LLM_MODELS_DIR" && -d "$LLM_MODELS_DIR" ]]; then
        rm -rf "$LLM_MODELS_DIR" && echo "  ✓ removed LLM models $LLM_MODELS_DIR"
    else
        echo "  ⏭ no LLM_MODELS_DIR to remove"
    fi
fi

# Credentials the operator placed by hand live under $STATE_DIR/creds — nothing
# in this repository writes them, so an uninstall has no business deleting them.
# It DOES have a duty to say they are still there: "everything goes" that quietly
# leaves an API key on disk is the sort of half-truth an operator acts on.
if [[ -d "$STATE_DIR/creds" ]] && [[ -n "$(ls -A "$STATE_DIR/creds" 2>/dev/null)" ]]; then
    echo
    ylw "⚠ CREDENTIALS LEFT ON THIS HOST — not created by this framework, so not"
    ylw "  removed by it. Delete them yourself if this host is being handed on:"
    for f in "$STATE_DIR/creds"/*; do [[ -e "$f" ]] && echo "     $f"; done
fi

echo
if [[ "${_data_failures:-0}" -gt 0 ]]; then
    red "Uninstall INCOMPLETE (level: $LEVEL) — $_data_failures data directory(ies)"
    red "could not be removed and STILL CONTAIN THE CORPUS (see above)."
    red "This host is not clean. Remove them, then re-run to verify."
    exit 1
fi
grn "Uninstall complete (level: $LEVEL)."
echo
ylw "⛔ NOT reversible. What survives, and why:"
echo "   host state  $STATE_DIR (backups, audit trail, capacity, baselines)"
echo "               restore with: ops/restore.sh, then"
echo "               scripts/update_framework.sh --from-restore"
echo "   checkout    $REPO_ROOT"
echo
echo "To remove the checkout too — run this yourself, it is the ground this"
echo "script was standing on:"
echo
echo "    rm -rf $REPO_ROOT"
echo
ylw "⚠ A reinstall MUST mint fresh tokens. The gateway .env held digests only;"
ylw "  every agent's raw token lived in the skill directories just deleted, so"
ylw "  restoring an old .env would configure auth nobody can satisfy."
