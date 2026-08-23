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
#            .env are untouched — this level is reversible by re-running
#            install_service.sh + sync_skills.sh.
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
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

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
        -h|--help)   sed -n '2,45p' "$0" | sed 's/^# \?//'; exit 0 ;;
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

# Agent skill directories: the REGISTRY first, because an install path is owned
# information about this host and not something a naming convention reproduces.
# The historical four are added only when they actually exist on disk.
mapfile -t _registry_dirs < <(
    grep -E '^AGENT_INSTALLS=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
    | tr ',' '\n' | sed -n 's/^[^:]*:\(.*\)$/\1/p' | xargs -r -n1 dirname 2>/dev/null)
SKILL_DIRS=()
for d in "${_registry_dirs[@]:-}"; do [[ -n "$d" && -d "$d" ]] && SKILL_DIRS+=("$d"); done
for d in "$HOME/.claude/skills/shared-memory" "$HOME/.codex/skills/shared-memory" \
         "$HOME/.gemini/skills/shared-memory" "$HOME/.grok/skills/shared-memory"; do
    [[ -d "$d" ]] || continue
    _dup=0; for s in ${SKILL_DIRS[@]+"${SKILL_DIRS[@]}"}; do [[ "$s" == "$d" ]] && _dup=1; done
    [[ "$_dup" == "0" ]] && SKILL_DIRS+=("$d")
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
    for d in "${SKILL_DIRS[@]}"; do echo "  agent skill dir   $d   ($(_size "$d"))  ⚠ contains that agent's raw token"; done
fi
if [[ "$LEVEL" == "data" || "$LEVEL" == "all" ]]; then
    echo "  containers        docker compose -f $(basename "$COMPOSE_FILE") down -v  (volumes included)"
    [[ -n "$NEO4J_HOST_DIR" ]] && echo "  neo4j data        $NEO4J_HOST_DIR   ($(_size "$NEO4J_HOST_DIR"))"
    [[ -n "$PG_DATA_DIR"    ]] && echo "  postgres data     $PG_DATA_DIR   ($(_size "$PG_DATA_DIR"))"
    echo "                    ⚠ bind mounts owned by the containers' uid — 'compose down -v'"
    echo "                      does NOT remove them, and this user usually cannot either;"
    echo "                      a throwaway root container does it, sudo is the fallback."
    echo "  gateway env       $ENV_FILE   ⚠ holds every credential this install has"
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
echo

if [[ "$DRY_RUN" == "1" ]]; then
    grn "Dry run — nothing was removed."
    exit 0
fi

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

if [[ "$ASSUME_YES" != "1" ]]; then
    echo
    read -r -p "Type the level ('$LEVEL') to confirm: " _answer
    [[ "$_answer" == "$LEVEL" ]] || die "not confirmed — nothing was removed."
fi

echo
# ── service ──────────────────────────────────────────────────────────────────
echo "Removing the service ..."
if command -v systemctl >/dev/null 2>&1; then
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
    echo "The stores, credentials and corpus are untouched. Reverse with:"
    echo "  bash shared-memory/ops/install_service.sh && bash shared-memory/scripts/sync_skills.sh"
    echo "  (each agent then needs its token re-issued: bootstrap_tokens.sh --remint <name>)"
    exit 0
}

# ── data ─────────────────────────────────────────────────────────────────────
echo "Removing containers and volumes ..."
if command -v docker >/dev/null 2>&1 && [[ -f "$COMPOSE_FILE" ]]; then
    docker compose -f "$COMPOSE_FILE" down -v 2>&1 | tail -3
    grn "  ✓ compose stack down, volumes removed"
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
