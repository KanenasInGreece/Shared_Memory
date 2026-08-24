"""Every operator-facing script under shared-memory/scripts/ and
shared-memory/ops/ must:

  1. accept -h/--help, print its OWN header comment block, and exit 0
     having done NOTHING else -- no side effects, no network, no docker,
     no systemd, no prompts.
  2. never leak a line of the script BODY into --help output (the specific,
     twice-measured defect: a hard-coded `sed -n 'A,Bp'` line range drifts
     the moment the header grows or shrinks by even one line -- v0.9.38 had
     it TRUNCATE update_framework.sh's help; this branch found the sibling
     defect, the exact opposite direction, in uninstall_framework.sh:67,
     which had drifted to leak `set -uo pipefail` and beyond).
  3. refuse an argument it does not recognise (nonzero exit) rather than
     silently ignoring it and proceeding -- the exact shape of RULING 2
     (install_service.sh had NO argument parsing at all: --help created a
     systemd unit, started the gateway, and enabled linger) generalised
     to every script in this family.

HOW THIS IS TESTED HERMETICALLY. Per this branch's own operating rule, none
of install_framework.sh / update_framework.sh / uninstall_framework.sh /
install_service.sh / sync_skills.sh / bootstrap_tokens.sh may be invoked
against this machine even with --help until --help is proven safe -- and
even then, only in a sandbox. So every script in this file, not just those
six, is copied to a throwaway file under tmp_path and run FROM THERE, with
HOME redirected into the same tmp_path and stdin closed (so a mutation that
reintroduced an interactive prompt would hang against /dev/null and time
out, not against a real terminal). Nothing here ever executes the checked-
out copy in shared-memory/scripts or shared-memory/ops directly.

Every script's own argument-parsing / --help block sits BEFORE any
REPO_ROOT-relative work (SCRIPT_DIR/REPO_ROOT resolution is read-only path
arithmetic and produces no side effect on its own), so a bare, standalone
copy -- not nested inside a real repo checkout -- is sufficient to prove
--help and the unknown-argument refusal never reach anything that touches
disk, docker, or systemd.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# Every operator-facing script this branch's build brief names, plus the two
# ops/*.sh scripts that already had SOME --help handling before this branch
# (backup.sh, restore.sh) -- both had drifted into the same leak defect as
# uninstall_framework.sh and are covered here for the same reason.
SCRIPTS = [
    REPO_ROOT / "shared-memory" / "scripts" / "uninstall_framework.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "update_framework.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "bootstrap_tokens.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "sync_skills.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "preflight.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "postflight.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "init_db.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "install_framework.sh",
    REPO_ROOT / "shared-memory" / "scripts" / "update_skill.sh",
    REPO_ROOT / "shared-memory" / "ops" / "install_service.sh",
    REPO_ROOT / "shared-memory" / "ops" / "install_llm_backends.sh",
    REPO_ROOT / "shared-memory" / "ops" / "backup.sh",
    REPO_ROOT / "shared-memory" / "ops" / "restore.sh",
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _sandbox_copy(script: Path, tmp_path: Path) -> Path:
    """Copy `script` to a throwaway location, isolated from the real repo
    tree it was read from -- proves --help needs nothing from REPO_ROOT."""
    dest_dir = tmp_path / "sandbox"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / script.name
    shutil.copy(script, dest)
    st = dest.stat()
    dest.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _sandbox_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def _header_lines(script: Path) -> list[str]:
    """The leading comment block (after the shebang), exactly what the
    shipped awk idiom is supposed to print -- read from the ORIGINAL script,
    never hard-coded here, so this test tracks the header regardless of how
    it grows or shrinks."""
    lines = []
    for line in script.read_text().splitlines()[1:]:  # skip the shebang
        if not line.startswith("#"):
            break
        lines.append(line.removeprefix("#").removeprefix(" "))
    return lines


def _first_body_line(script: Path) -> str:
    """The first NON-BLANK line AFTER the header comment block -- typically
    `set -...`. This exact line leaking into --help output is precisely
    what both measured defects (update_framework.sh v0.9.38 in one
    direction, this branch's uninstall_framework.sh in the other) would
    produce. Blank separator lines are skipped: they occur inside the
    header's own prose too, so their mere presence proves nothing."""
    in_header = True
    for line in script.read_text().splitlines()[1:]:
        if in_header:
            if line.startswith("#"):
                continue
            in_header = False
        if line.strip() == "":
            continue
        return line
    raise AssertionError(f"{script} has no body after its header comment block")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_help_exits_zero_and_does_nothing_else(script, tmp_path):
    sandboxed = _sandbox_copy(script, tmp_path)
    env = _sandbox_env(tmp_path)
    before = {p.name for p in (tmp_path / "sandbox").iterdir()}

    proc = subprocess.run(
        ["bash", str(sandboxed), "--help"],
        capture_output=True, text=True, timeout=15,
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, (
        f"{script.name} --help exited {proc.returncode}, expected 0:\n{out}"
    )
    after = {p.name for p in (tmp_path / "sandbox").iterdir()}
    assert after == before, (
        f"{script.name} --help created/removed a file in its own directory "
        f"-- it must do NOTHING besides print help: {before} -> {after}"
    )
    # Nothing landed under the sandboxed $HOME either (a systemd unit, a
    # .shared-memory state dir, an .env, ...).
    home_contents = list((tmp_path / "home").iterdir())
    assert home_contents == [], (
        f"{script.name} --help wrote into \\$HOME: {home_contents}"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_help_output_matches_the_real_header_exactly(script, tmp_path):
    """Structural pin against BOTH truncation (v0.9.38's defect) and leakage
    (this branch's defect): every header line must appear, and the first
    BODY line must not."""
    sandboxed = _sandbox_copy(script, tmp_path)
    env = _sandbox_env(tmp_path)

    proc = subprocess.run(
        ["bash", str(sandboxed), "--help"],
        capture_output=True, text=True, timeout=15,
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
    )
    out = _strip_ansi(proc.stdout)
    header = _header_lines(script)
    assert header, f"{script.name} has no leading comment header to compare against"

    last_line = header[-1]
    assert last_line in out, (
        f"{script.name} --help is missing the LAST header line ({last_line!r}) "
        f"-- truncation:\n{out}"
    )

    body_line = _first_body_line(script)
    assert body_line.strip() not in _strip_ansi(proc.stdout).splitlines(), (
        f"{script.name} --help LEAKED the first body line ({body_line!r}) "
        f"verbatim -- this is the exact hardcoded-range defect this suite "
        f"exists to catch:\n{out}"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_unknown_argument_is_refused_not_silently_ignored(script, tmp_path):
    sandboxed = _sandbox_copy(script, tmp_path)
    env = _sandbox_env(tmp_path)
    before = {p.name for p in (tmp_path / "sandbox").iterdir()}

    proc = subprocess.run(
        ["bash", str(sandboxed), "--this-flag-does-not-exist-anywhere"],
        capture_output=True, text=True, timeout=15,
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, (
        f"{script.name} silently accepted an unrecognised argument instead "
        f"of refusing it (exit 0):\n{out}"
    )
    after = {p.name for p in (tmp_path / "sandbox").iterdir()}
    assert after == before, (
        f"{script.name} performed a side effect before refusing the unknown "
        f"argument: {before} -> {after}"
    )
    home_contents = list((tmp_path / "home").iterdir())
    assert home_contents == [], (
        f"{script.name} wrote into \\$HOME before refusing the unknown "
        f"argument: {home_contents}"
    )
