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
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

# ── Linger: keeps user services running with no active login session ───────
# (survives logout/reboot) -- see D18 below.
#
# `loginctl show-user "$USER" --property=Linger` reads systemd-logind's OWN
# record of the flag -- the only source of truth this function trusts. It
# is consulted at the END, never inferred from either enable-linger
# invocation's exit status: a fresh-host finding (D18) was that unprivileged
# `loginctl enable-linger` fails with "Could not enable linger: Access
# denied" on a NON-INTERACTIVE session (no polkit agent — e.g. a script run
# over a plain SSH session with no active seat), the OLD code ran it,
# ignored the failure entirely, and then unconditionally printed
# "✓ Linger enabled for $USER" — a lie. Without linger, `systemd --user` is
# torn down the moment the install session ends and the gateway dies on
# logout, which is the ONE failure mode this whole script exists to
# prevent, so a script that reports success without checking is worse than
# one that says nothing.
# >>> ENABLE_LINGER
enable_linger() {
    # 1. Unprivileged attempt — works whenever polkit grants it directly
    #    (the common case: an interactive desktop or SSH login).
    loginctl enable-linger "$USER" >/dev/null 2>&1 || true

    # 2. Non-interactive sudo retry (-n: fail immediately, never prompt for
    #    a password — a script blocking on a hidden prompt is worse than
    #    failing loudly). Covers a host where the operator has passwordless
    #    sudo but no polkit agent (a bare-metal/CI install, the case D18
    #    was actually measured on).
    if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -qx "Linger=yes"; then
        sudo -n loginctl enable-linger "$USER" >/dev/null 2>&1 || true
    fi

    # 3. VERIFY the real end state, never trust either exit status — immune
    #    both to the silent no-op above and to a loginctl/sudo combination
    #    that exits 0 without actually flipping the flag.
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

# Best-effort — the Documentation= line is informational only, a placeholder is fine.
# git remote get-url may return an SSH form (git@github.com:user/repo.git), which
# isn't a browsable URL; convert the common case, else fall back to the placeholder.
_raw_remote="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
REPO_URL="$(printf '%s' "$_raw_remote" | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##')"
[[ "$REPO_URL" == https://* ]] || REPO_URL="https://github.com/YOUR_GITHUB_USER/shared-memory"

# Resolve the uv this host actually has. The unit template's /usr/bin/uv is a
# placeholder: the documented uv install (https://astral.sh/uv) lands in
# ~/.local/bin, which is neither the template path nor on a systemd unit's
# default PATH — an unsubstituted unit crash-loops with 203/EXEC, and even with
# ExecStart fixed the gateway's daemon spawns (shutil.which("uv")) come up
# empty, leaving consolidation/REM silently stopped. Substitute both.
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
echo "  Logs:    journalctl --user -u hive-mind-gateway.service -f"
echo
echo "  Note: the gateway needs the Docker stack + tokens from earlier Quick Start"
echo "  steps to actually serve traffic. Restart=on-failure means it retries quietly"
echo "  until those are in place — nothing more to do once those steps are done."
echo
echo "  Want a reasoning-LLM backend (local-supervised, remote, or a paid cloud"
echo "  API) configured too? bash shared-memory/ops/install_llm_backends.sh"

[[ "$LINGER_OK" -eq 1 ]] || exit 1
