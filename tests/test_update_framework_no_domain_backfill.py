"""update_framework.sh — --no-domain-backfill (branch fix/domain-backfill-opt-out).

WHY THIS EXISTS. Step 6 of update_framework.sh enqueues the domain-backfill
migration (backfill_domain_of.py) unconditionally on every upgrade run. An
operator who wants everything else this run does -- fetch, schema
migrations, gateway restart, skill sync, postflight -- WITHOUT also
triggering that one migration on this pass had no lever for it: the only
existing knob was --dry-run, which declines EVERYTHING, not just step 6.
--no-domain-backfill adds a narrow, single-run opt-out for that one step;
default behaviour (the flag absent) must be provably unchanged, and step
numbers after step 6 must not drift either way.

⚠ EXECUTABLE, not source-reading: every test drives the REAL shipped script
via subprocess -- see tests/test_preflight_uv_path_check.py and
tests/test_install_service_linger.py for the established pattern -- never a
hand-written reimplementation that could silently drift from it.

To keep runs hermetic and off the live production checkout's own .env /
git state, the script is exercised from a throwaway sandbox: a tmp
directory holding a bare `git init`, a dummy shared-memory/.env, and a
SYMLINK (never a copy) to the actual shipped update_framework.sh, placed at
the path its own REPO_ROOT resolution expects
(shared-memory/scripts/update_framework.sh relative to the repo root).
Bash resolves ${BASH_SOURCE[0]} to the path it was INVOKED with, not the
symlink's target, so REPO_ROOT lands inside the sandbox and every later
`$REPO_ROOT/...` reference in the script follows it there too.

Every run in this file passes --dry-run --skip-backup. In --dry-run mode,
run()/run_soft() print the step's command line and return WITHOUT ever
executing it -- covering git pull, both migration runners, the gateway
restart, sync_skills.sh, postflight.sh, and (the thing these tests actually
check) the domain-backfill invocation itself. So no real infrastructure
(a git remote, Postgres, Neo4j, the gateway, curl) is ever touched, and the
presence or absence of the printed "... backfill_domain_of.py ..." command
line in stdout is a faithful proxy for whether that step would have
executed for real -- it is produced by the exact same
`if NO_DOMAIN_BACKFILL ... elif DRY_RUN ... else` selection a live run uses.
--skip-backup sidesteps an unrelated shared-memory/ops/backup.sh existence
requirement that has nothing to do with this flag.
"""
import os
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).parent.parent
_REAL_SCRIPT = _REPO / "shared-memory" / "scripts" / "update_framework.sh"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _make_sandbox(tmp_path: Path) -> Path:
    """A throwaway repo root: bare git init, dummy .env, and a symlink to
    the real script at the path its own REPO_ROOT math expects -- never a
    copy, so there is no chance of the sandboxed run drifting from the
    shipped source.

    Also gives the sandbox a real branch (pinned "main", never whatever
    init.defaultBranch this workstation happens to have), one commit, and a
    local ("origin") bare remote with upstream tracking configured via
    `git push -u`. Step 0's pre-`git pull` guard (RULING A / fact-driven
    fix alongside test_update_framework_branch_guard.py) refuses BEFORE
    `git pull` -- even under --dry-run, since it is plain script logic
    outside run()'s own DRY_RUN gate -- when the current branch has no
    upstream, or when it does but the branch is gone from the remote. A
    sandbox with neither a remote nor upstream tracking would trip that
    refusal on every test in this file, none of which are about branch
    state at all -- see test_update_framework_branch_guard.py for the
    guard's own dedicated tests."""
    repo = tmp_path / "repo"
    scripts_dir = repo / "shared-memory" / "scripts"
    scripts_dir.mkdir(parents=True)
    (repo / "shared-memory" / ".env").write_text("DUMMY=1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                    cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "sandbox@example.invalid"],
                    cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "sandbox"],
                    cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)],
                    cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                    cwd=repo, check=True)
    link = scripts_dir / "update_framework.sh"
    link.symlink_to(_REAL_SCRIPT.resolve())
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    return subprocess.run(
        ["bash", str(repo / "shared-memory" / "scripts" / "update_framework.sh"), *args],
        capture_output=True, text=True, timeout=30, cwd=repo, env=env,
    )


def _step_lines(stdout: str):
    """[(step_number, label), ...] in order, parsed from the script's own
    "── Step N: label" progress lines."""
    clean = _strip_ansi(stdout)
    return [(int(n), label.strip()) for n, label in
            re.findall(r"── Step (\d+): (.+)", clean)]


def _after_backfill(steps):
    for i, (_n, label) in enumerate(steps):
        if label.startswith("domain backfill"):
            return steps[i + 1:]
    raise AssertionError(f"no 'domain backfill' step found in {steps}")


# ── 1. the flag parses and is accepted ───────────────────────────────────

def test_flag_parses_and_is_accepted(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "unknown argument" not in out


# ── 2. with the flag, the backfill is NOT run ────────────────────────────

def test_with_flag_backfill_is_skipped(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "SKIPPED — --no-domain-backfill given" in out
    assert "backfill_domain_of.py" not in out, (
        "the backfill command line was printed even with --no-domain-backfill "
        "given -- the skip branch must never reach run()"
    )


# ── 3. WITHOUT the flag, the backfill IS still run (default unchanged) ──

def test_without_flag_backfill_still_runs_by_default(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" in out, (
        "the default run (flag absent) no longer queues the domain backfill "
        "-- the opt-out flag must not have changed default behaviour"
    )
    assert "SKIPPED — --no-domain-backfill given" not in out


# ── 4. step numbers for steps AFTER step 6 are identical either way ─────

def test_step_numbering_after_backfill_is_unchanged_either_way(tmp_path):
    repo_a = _make_sandbox(tmp_path / "a")
    repo_b = _make_sandbox(tmp_path / "b")
    with_flag = _run(repo_a, "--dry-run", "--skip-backup", "--no-domain-backfill")
    without_flag = _run(repo_b, "--dry-run", "--skip-backup")
    assert with_flag.returncode == 0, with_flag.stdout
    assert without_flag.returncode == 0, without_flag.stdout

    steps_with = _step_lines(with_flag.stdout)
    steps_without = _step_lines(without_flag.stdout)

    # The domain-backfill step's own label legitimately differs between the
    # two modes (SKIPPED vs preview) -- that is not what this test is about.
    # What must not move is everything AFTER it: sync_skills, postflight, and
    # the same step numbers on each.
    tail_with = _after_backfill(steps_with)
    tail_without = _after_backfill(steps_without)
    assert tail_with == tail_without, (
        "steps after the domain backfill diverge between the two modes:\n"
        f"  with --no-domain-backfill:    {tail_with}\n"
        f"  without --no-domain-backfill: {tail_without}"
    )
    # And there IS a tail to compare -- an empty tail would make the equality
    # above vacuously true and prove nothing.
    assert tail_with, "no steps were recorded after the domain backfill step"


# ── TV-4: interaction with --from-restore ────────────────────────────────
# --from-restore skips step 0 (fetching code) but otherwise runs the same
# procedure -- the domain-backfill selection logic in step 6 does not read
# FROM_RESTORE at all, so the flag must behave identically whether or not
# --from-restore is also given. Untested before this review.

def test_no_domain_backfill_skips_with_from_restore_too(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--from-restore", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "SKIPPED — --no-domain-backfill given" in out
    assert "backfill_domain_of.py" not in out, (
        "--no-domain-backfill did not skip the backfill when combined with "
        "--from-restore"
    )


def test_domain_backfill_still_runs_with_from_restore_and_no_flag(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--from-restore")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" in out, (
        "--from-restore alone (no --no-domain-backfill) unexpectedly "
        "suppressed the default backfill"
    )
    assert "SKIPPED — --no-domain-backfill given" not in out


# ── 5. an unknown argument is still rejected ─────────────────────────────

def test_unknown_argument_still_rejected(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--not-a-real-flag")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode != 0
    assert "unknown argument: --not-a-real-flag" in out


# ── --help lists the new flag and keeps the exit-contract sentence ──────

def test_help_documents_the_flag_and_keeps_exit_contract():
    """Not sandboxed -- --help exits inside the argument-parsing loop, before
    any REPO_ROOT/.env work happens, so it is safe to run directly against
    the real script without a sandbox.

    TV-3/TV-5 (Test_Verification_Review.md): the OLD version of this test
    asserted only a substring from the MIDDLE of the header ("Exit 0 only
    when postflight passes.") -- a sentence that survives even if the FINAL
    header line gets silently truncated by a future one-line header growth,
    because it isn't the last thing printed. That is exactly the failure
    already observed once (the sed range hard-coded '2,32p'). --help itself
    was changed to a dynamic boundary (an awk scan that prints every leading
    '#' line and stops at the first non-'#' line, so it can never truncate
    regardless of header length) -- this test now pins that fix from the
    OUTPUT side, by asserting the LAST line of the header comment block
    specifically, so a regression to a hard-coded range (or any other
    truncation) is caught structurally rather than by coincidence."""
    proc = subprocess.run(
        ["bash", str(_REAL_SCRIPT), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--no-domain-backfill" in out
    assert "Exit 0 only when postflight passes." in out, (
        "the --help output no longer reaches the exit-contract sentence"
    )

    # The header comment block's own LAST line, read directly from the
    # script rather than hard-coded here, so this test tracks the header
    # even if it grows or shrinks.
    header_lines = []
    for line in _REAL_SCRIPT.read_text().splitlines()[1:]:  # skip shebang
        if not line.startswith("#"):
            break
        header_lines.append(line)
    last_header_line = header_lines[-1].removeprefix("#").removeprefix(" ")
    assert last_header_line, "could not determine the header's last line"
    assert last_header_line in out, (
        f"--help output is missing the LAST line of the header comment "
        f"block ({last_header_line!r}) -- this is exactly the truncation "
        f"TV-3/TV-5 describes: a mid-header substring can pass while the "
        f"tail is silently cut off\nfull output:\n{out}"
    )
