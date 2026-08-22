"""install_service.sh — enable_linger() (fresh-host finding D18).

Only the deterministic enable_linger() function is testable here. It is
embedded in shared-memory/ops/install_service.sh (between the
`# >>> ENABLE_LINGER` / `# <<< ENABLE_LINGER` markers) rather than
duplicated: this file extracts that block VERBATIM and runs it standalone
via subprocess, with fake `loginctl`/`sudo` stubs placed first on PATH, so
the test exercises the actual shipped source against controlled scenarios
-- never a hand-written reimplementation that could silently drift from it.

D18: on a fresh Debian 13 host, unprivileged `loginctl enable-linger`
failed with "Could not enable linger: Access denied" (no polkit agent on a
non-interactive session), the script continued anyway, and unconditionally
printed "Linger enabled" -- a lie. Without linger, systemd --user is torn
down when the last session ends and the gateway dies on logout. The fix:
try unprivileged, retry with `sudo -n` (never prompts), then VERIFY the
real end state via `loginctl show-user --property=Linger` and report
failure (nonzero) rather than trusting either command's exit status.

Everything else install_service.sh does (unit-file templating, systemctl
enable/daemon-reload, the informational REPO_URL best-effort) needs a real
systemd --user manager and is NOT exercised here -- this file is scoped to
the one fresh-host defect the build brief actually names.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

INSTALL_SERVICE = (
    Path(__file__).parent.parent / "shared-memory" / "ops" / "install_service.sh"
)

BEGIN_MARKER = "# >>> ENABLE_LINGER"
END_MARKER = "# <<< ENABLE_LINGER"


def _extract_enable_linger_source() -> str:
    text = INSTALL_SERVICE.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{INSTALL_SERVICE} -- the extraction markers moved or were removed"
    )
    return m.group(1)


_LOGINCTL_STUB = """#!/usr/bin/env bash
# Simulates systemd-logind's linger property: $LINGER_STATE_FILE holds
# "yes" or "no", read/written the same way the real property store would
# be. $LOGINCTL_ENABLE_MODE controls what `enable-linger` does:
#   succeed -- flips the state file to yes, exit 0 (normal interactive case)
#   fail    -- "Access denied" on stderr, exit 1, state untouched (D18)
#   lie     -- exit 0 WITHOUT flipping the state file (tests that
#              enable_linger() verifies rather than trusting the exit code)
case "$1" in
    enable-linger)
        case "${LOGINCTL_ENABLE_MODE:-fail}" in
            succeed) echo yes > "$LINGER_STATE_FILE"; exit 0 ;;
            lie)     exit 0 ;;
            *)       echo "Could not enable linger: Access denied" >&2; exit 1 ;;
        esac
        ;;
    show-user)
        state="$(cat "$LINGER_STATE_FILE" 2>/dev/null || echo no)"
        echo "Linger=$state"
        exit 0
        ;;
    *)
        echo "fake loginctl: unhandled args: $*" >&2
        exit 1
        ;;
esac
"""

_SUDO_STUB = """#!/usr/bin/env bash
# Simulates `sudo -n loginctl enable-linger $USER` -- $SUDO_MODE controls
# whether passwordless sudo is available on this (fake) host.
[[ "$1" == "-n" ]] && shift
case "${SUDO_MODE:-fail}" in
    succeed) echo yes > "$LINGER_STATE_FILE"; exit 0 ;;
    *)       echo "sudo: a password is required" >&2; exit 1 ;;
esac
"""


def _run_enable_linger(tmp_path, env_overrides: dict) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    loginctl = bin_dir / "loginctl"
    sudo = bin_dir / "sudo"
    loginctl.write_text(_LOGINCTL_STUB)
    sudo.write_text(_SUDO_STUB)
    loginctl.chmod(loginctl.stat().st_mode | stat.S_IEXEC)
    sudo.chmod(sudo.stat().st_mode | stat.S_IEXEC)

    state_file = tmp_path / "linger_state"
    source = _extract_enable_linger_source()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["USER"] = env.get("USER", "testuser")
    env["LINGER_STATE_FILE"] = str(state_file)
    env.update(env_overrides)

    return subprocess.run(
        ["bash", "-c", source + "\nenable_linger"],
        capture_output=True, text=True, timeout=15, env=env,
    )


def test_markers_present_exactly_once():
    text = INSTALL_SERVICE.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_the_function():
    source = _extract_enable_linger_source()
    assert "enable_linger()" in source


def test_unprivileged_success_reports_success(tmp_path):
    proc = _run_enable_linger(
        tmp_path, {"LOGINCTL_ENABLE_MODE": "succeed", "SUDO_MODE": "fail"},
    )
    assert proc.returncode == 0
    assert (tmp_path / "linger_state").read_text().strip() == "yes"


def test_unprivileged_failure_falls_back_to_sudo_and_succeeds(tmp_path):
    """D18's actual measured scenario: unprivileged loginctl fails (no
    polkit agent), the sudo -n retry succeeds."""
    proc = _run_enable_linger(
        tmp_path, {"LOGINCTL_ENABLE_MODE": "fail", "SUDO_MODE": "succeed"},
    )
    assert proc.returncode == 0
    assert (tmp_path / "linger_state").read_text().strip() == "yes"


def test_both_unprivileged_and_sudo_fail_reports_failure(tmp_path):
    """Neither path works (no polkit, no passwordless sudo) -- the function
    must report FAILURE (nonzero), never claim success."""
    proc = _run_enable_linger(
        tmp_path, {"LOGINCTL_ENABLE_MODE": "fail", "SUDO_MODE": "fail"},
    )
    assert proc.returncode != 0
    assert not (tmp_path / "linger_state").exists()


def test_never_trusts_a_lying_exit_code_from_enable_linger(tmp_path):
    """I-A6 / D18's central guard: enable_linger() must not take EITHER
    command's exit status as truth -- it must VERIFY via show-user. A
    `loginctl enable-linger` that exits 0 without actually flipping the
    property (the "lie" mode) must still be reported as a FAILURE."""
    proc = _run_enable_linger(
        tmp_path, {"LOGINCTL_ENABLE_MODE": "lie", "SUDO_MODE": "fail"},
    )
    assert proc.returncode != 0


def test_lying_unprivileged_call_still_tries_the_sudo_fallback(tmp_path):
    """A lying unprivileged call must not be mistaken for success and skip
    the sudo retry -- the verify-then-fallback sequencing matters, not just
    the final answer."""
    proc = _run_enable_linger(
        tmp_path, {"LOGINCTL_ENABLE_MODE": "lie", "SUDO_MODE": "succeed"},
    )
    assert proc.returncode == 0
    assert (tmp_path / "linger_state").read_text().strip() == "yes"
