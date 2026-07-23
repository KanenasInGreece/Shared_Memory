#!/usr/bin/env bash
#
# install_service.sh — install/enable the Hive-Mind gateway as a systemd --user
# service, so it starts at boot and shuts down cleanly at power-off with no
# manual step after every restart. Idempotent — safe to re-run.
#
#   bash shared-memory/ops/install_service.sh
#
# Automates the "Install" steps documented by hand in shared-memory/ops/README.md,
# "hive-mind-gateway.service" — that section explains WHY a service (a
# session-launched gateway is killed on logout, nohup does not help).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # …/shared-memory/ops
FRAMEWORK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                 # …/shared-memory
REPO_DIR="$(cd "$FRAMEWORK_DIR/.." && pwd)"                   # repo root
UNIT_SRC="$SCRIPT_DIR/hive-mind-gateway.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DST_DIR/hive-mind-gateway.service"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }

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

# Best-effort — the Documentation= line is informational only, a placeholder is fine.
# git remote get-url may return an SSH form (git@github.com:user/repo.git), which
# isn't a browsable URL; convert the common case, else fall back to the placeholder.
_raw_remote="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
REPO_URL="$(printf '%s' "$_raw_remote" | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##')"
[[ "$REPO_URL" == https://* ]] || REPO_URL="https://github.com/YOUR_GITHUB_USER/shared-memory"

sed -e "s#/path/to/your/shared-memory-GitHub#$REPO_DIR#" \
    -e "s#https://github.com/YOUR_GITHUB_USER/shared-memory#$REPO_URL#" \
    "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now hive-mind-gateway.service
# Makes user services keep running with no active login session (survives logout/reboot).
loginctl enable-linger "$USER"

echo
grn "✓ Installed $UNIT_DST"
grn "✓ Enabled + started hive-mind-gateway.service"
grn "✓ Linger enabled for $USER — the gateway now starts at boot and stops cleanly"
echo "  at shutdown, no login session required, no manual restart step."
echo
echo "  Verify:  systemctl --user status hive-mind-gateway.service"
echo "           curl -s localhost:8888/health"
echo "  Logs:    journalctl --user -u hive-mind-gateway.service -f"
echo
echo "  Note: the gateway needs the Docker stack + tokens from earlier Quick Start"
echo "  steps to actually serve traffic. Restart=on-failure means it retries quietly"
echo "  until those are in place — nothing more to do once those steps are done."
echo
echo "  Want a reasoning-LLM backend (local-supervised, remote, or a paid cloud"
echo "  API) configured too? bash shared-memory/ops/install_llm_backends.sh"
