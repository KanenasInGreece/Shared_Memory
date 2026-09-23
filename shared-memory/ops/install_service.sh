#!/usr/bin/env bash
#
# install_service.sh — install the gateway as a systemd --user service so it survives logout. Idempotent. A session-launched process dies on logout, and nohup does not help.
#
#   bash shared-memory/ops/install_service.sh

set -euo pipefail

# --help used to create the unit, start the gateway, and enable linger. Unknown arguments must refuse.
for _arg in "$@"; do
    case "$_arg" in
        -h|--help)
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
            exit 0
            ;;
    esac
done
if [[ $# -gt 0 ]]; then
    printf '\033[31m%s\033[0m\n' "✗ unknown argument: $1 (this script takes none — see --help)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # …/shared-memory/ops
FRAMEWORK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                 # …/shared-memory
REPO_DIR="$(cd "$FRAMEWORK_DIR/.." && pwd)"                   # repo root
UNIT_SRC="$SCRIPT_DIR/hive-mind-gateway.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DST_DIR/hive-mind-gateway.service"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

# Linger keeps the user manager alive after logout. Trust show-user, not enable-linger's exit status: a non-interactive session is denied and the old script still printed success, so the gateway died on logout.
# >>> ENABLE_LINGER
enable_linger() {
    # Unprivileged first. Works when polkit grants it.
    loginctl enable-linger "$USER" >/dev/null 2>&1 || true

    # -n so a missing passwordless sudo fails instead of hanging on a hidden prompt.
    if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -qx "Linger=yes"; then
        sudo -n loginctl enable-linger "$USER" >/dev/null 2>&1 || true
    fi

    # Exit status can lie. The property is the only success.
    loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -qx "Linger=yes"
}
# <<< ENABLE_LINGER

[[ -f "$UNIT_SRC" ]] || { red "ERROR: missing $UNIT_SRC"; exit 1; }

command -v systemctl >/dev/null 2>&1 || {
    red "ERROR: systemctl not found — this host does not run systemd."
    echo "  The gateway still runs fine started by hand; it just won't survive"
    echo "  logout/reboot without systemd. Use your platform's own service manager."
    exit 1
}
systemctl --user status >/dev/null 2>&1 || {
    red "ERROR: systemd --user manager not reachable."
    echo "  Log in via a normal graphical or SSH session (not su/sudo, not a"
    echo "  container without a login session) and retry."
    exit 1
}

echo "── Shared Memory — install the gateway as a systemd --user service ──"
mkdir -p "$UNIT_DST_DIR"

# Documentation= is informational. An SSH remote is rewritten to https when the shape is the common one.
_raw_remote="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
REPO_URL="$(printf '%s' "$_raw_remote" | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##')"
[[ "$REPO_URL" == https://* ]] || REPO_URL="https://github.com/YOUR_GITHUB_USER/shared-memory"

# The unit's /usr/bin/uv is a placeholder. An unsubstituted path crash-loops 203/EXEC, and a missing PATH entry leaves the daemons stopped while the gateway looks healthy.
UV_BIN="$(command -v uv || true)"
[[ -x "$UV_BIN" ]] || UV_BIN="$HOME/.local/bin/uv"
[[ -x "$UV_BIN" ]] || UV_BIN="$HOME/.cargo/bin/uv"
[[ -x "$UV_BIN" ]] || {
    red "ERROR: uv not found (PATH, ~/.local/bin, ~/.cargo/bin)."
    echo "  Install it first: https://docs.astral.sh/uv/ — then re-run this script."
    exit 1
}
UV_DIR="$(dirname "$UV_BIN")"

sed -e "s#/path/to/your/shared-memory-GitHub#$REPO_DIR#" \
    -e "s#https://github.com/YOUR_GITHUB_USER/shared-memory#$REPO_URL#" \
    -e "s#^ExecStart=/usr/bin/uv #ExecStart=$UV_BIN #" \
    -e "s#^Environment=PATH=#Environment=PATH=$UV_DIR:#" \
    "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now hive-mind-gateway.service

if enable_linger; then
    LINGER_OK=1
else
    LINGER_OK=0
fi

echo
grn "✓ Installed $UNIT_DST"
grn "✓ Enabled + started hive-mind-gateway.service"
if [[ "$LINGER_OK" -eq 1 ]]; then
    grn "✓ Linger enabled for $USER — the gateway now starts at boot and stops cleanly"
    echo "  at shutdown, no login session required, no manual restart step."
else
    red "✗ Linger could NOT be enabled for $USER (D18: no polkit agent on this"
    red "  session, and passwordless sudo isn't available either)."
    echo "  Without it, systemd --user is torn down the moment THIS session ends —"
    echo "  the gateway will be KILLED when your last session ends, exactly the"
    echo "  failure this service exists to prevent. Run this yourself, in a session"
    echo "  with a real terminal (it will prompt for your password):"
    echo
    echo "    sudo loginctl enable-linger $USER"
    echo
    echo "  Then verify:  loginctl show-user $USER --property=Linger"
    echo "  (expect Linger=yes)"
fi
echo
echo "  Verify:  systemctl --user status hive-mind-gateway.service"
echo "           curl -s localhost:8888/health"
echo "           loginctl show-user $USER --property=Linger   (expect Linger=yes —"
echo "           'systemctl status' can read active/running from THIS session even after"
echo "           linger is lost; only the linger flag protects the gateway past logout)"
echo "  Logs:    journalctl --user -u hive-mind-gateway.service -f"
echo
echo "  Note: the gateway needs the Docker stack + tokens from earlier Quick Start"
echo "  steps to actually serve traffic. Restart=on-failure means it retries quietly"
echo "  until those are in place — nothing more to do once those steps are done."
echo
echo "  Want a reasoning-LLM backend (local-supervised, remote, or a paid cloud"
echo "  API) configured too? bash shared-memory/ops/install_llm_backends.sh"

[[ "$LINGER_OK" -eq 1 ]] || exit 1
