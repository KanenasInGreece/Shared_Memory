"""update_framework.sh — the domain-backfill step is OPT-IN (v0.9.69,
fact:1734 C(d) / decision:1736 / O1 — "no orchestration script applies an
axis rewrite unless the operator asked for that run").

RE-RULED (v0.9.69) — this file used to pin the OPPOSITE default: step 6 ran
UNCONDITIONALLY unless `--no-domain-backfill` was given. That is precisely
the accident O1 exists to prevent — a script that rewrites an axis on every
deployment without the operator asking for it on THAT invocation. The
default is now SKIP; `--domain-backfill` opts in for one run;
`--no-domain-backfill` is kept for one release as a documented no-op (the
skip it used to request is now the default) so an existing invocation does
not start failing on an unrecognised flag.

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
`if DOMAIN_BACKFILL != 1 ... elif DRY_RUN ... else` selection a live run uses.
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


# ── 1. both flags parse and are accepted ─────────────────────────────────

def test_domain_backfill_flag_parses_and_is_accepted(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--domain-backfill")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "unknown argument" not in out


def test_no_domain_backfill_flag_still_parses_as_a_noop(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "unknown argument" not in out


# ── 2. by DEFAULT (neither flag), the backfill is NOT run ────────────────

def test_default_skips_the_backfill(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "SKIPPED — opt-in as of this release" in out
    assert "backfill_domain_of.py" not in out, (
        "the default run (no flag) queued the domain backfill -- it is "
        "opt-in as of this release, not opt-out"
    )


# ── 3. WITH --domain-backfill, the backfill IS run ───────────────────────

def test_domain_backfill_flag_runs_it(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" in out, (
        "--domain-backfill did not queue the domain backfill step"
    )
    assert "SKIPPED — opt-in as of this release" not in out


# ── 4. --no-domain-backfill alone stays a no-op: still skipped, plus notice ─

def test_no_domain_backfill_alone_is_still_skipped_and_prints_the_notice(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" not in out, (
        "--no-domain-backfill (alone) unexpectedly queued the backfill -- "
        "it must remain a no-op"
    )
    assert "Notice: --no-domain-backfill" in out, (
        "the one-release deprecation notice for --no-domain-backfill did "
        "not print"
    )
    assert "no-op" in out


def test_domain_backfill_wins_when_both_flags_are_given(tmp_path):
    """--domain-backfill is the explicit ask this run; --no-domain-backfill
    is a legacy no-op, not a veto -- an operator who passes both (e.g. a
    half-updated script wrapper) gets the explicit opt-in honoured, plus the
    deprecation notice naming the no-op flag."""
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--domain-backfill", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" in out
    assert "Notice: --no-domain-backfill" in out


# ── 5. the dry-run preview states what a REAL (opted-in) run actually does ──

def test_dry_run_preview_shows_the_apply_flag_a_real_run_would_use(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py --apply" in out, (
        "the dry-run preview does not show the --apply flag a real run "
        "would actually pass -- it understates what the real run does\n"
        f"{out}"
    )


# NOTE: test_update_framework_live_execution.py's
# test_live_run_with_domain_backfill_flag_invokes_backfill_with_apply
# independently pins the REAL (non-dry-run) invocation as
# `backfill_domain_of.py ... --apply` -- the same substring
# test_dry_run_preview_shows_the_apply_flag_a_real_run_would_use above
# asserts for the DRY-RUN preview, so the two together prove the preview
# matches what a real run actually executes rather than merely containing
# some --apply invocation.


# ── 6. step numbers for steps AFTER step 6 are identical either way ──────

def test_step_numbering_after_backfill_is_unchanged_either_way(tmp_path):
    repo_a = _make_sandbox(tmp_path / "a")
    repo_b = _make_sandbox(tmp_path / "b")
    with_flag = _run(repo_a, "--dry-run", "--skip-backup", "--domain-backfill")
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
        f"  with --domain-backfill:    {tail_with}\n"
        f"  without any flag:          {tail_without}"
    )
    # And there IS a tail to compare -- an empty tail would make the equality
    # above vacuously true and prove nothing.
    assert tail_with, "no steps were recorded after the domain backfill step"


# ── TV-4: interaction with --from-restore ────────────────────────────────
# --from-restore skips step 0 (fetching code) but otherwise runs the same
# procedure -- the domain-backfill selection logic in step 6 does not read
# FROM_RESTORE at all, so the flags must behave identically whether or not
# --from-restore is also given.

def test_domain_backfill_flag_runs_with_from_restore_too(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--from-restore", "--domain-backfill")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" in out, (
        "--domain-backfill did not queue the backfill when combined with "
        "--from-restore"
    )


def test_default_still_skips_with_from_restore_and_no_flag(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--dry-run", "--skip-backup", "--from-restore")
    out = _strip_ansi(proc.stdout)
    assert proc.returncode == 0, out
    assert "backfill_domain_of.py" not in out, (
        "--from-restore alone (no --domain-backfill) unexpectedly ran the "
        "opt-in backfill"
    )
    assert "SKIPPED — opt-in as of this release" in out


# ── 7. an unknown argument is still rejected ─────────────────────────────

def test_unknown_argument_still_rejected(tmp_path):
    repo = _make_sandbox(tmp_path)
    proc = _run(repo, "--not-a-real-flag")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode != 0
    assert "unknown argument: --not-a-real-flag" in out


# ── --help lists both flags and keeps the exit-contract sentence ────────

def test_help_documents_the_flags_and_keeps_exit_contract():
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
    assert "--domain-backfill" in out
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
