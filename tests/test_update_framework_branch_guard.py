"""update_framework.sh -- RULING A / RULING B: step 0's pre-`git pull` branch
checks, added after a cold agent ran the published upgrade procedure on a
real host and it FAILED at the first step with git's own raw error:

    Your configuration specifies to merge with the ref
    'refs/heads/fix/some-merged-feature' from the remote, but no such
    ref was fetched.

RULING A (CRITICAL). Cause: the remote branch had been DELETED when its PR
merged (this repo squash-merges and deletes branches on merge), so the local
upstream config still names a ref `git pull --ff-only` cannot resolve. The
detached-HEAD and tarball-tree refusals already in step 0 exist precisely so
a bad state reads as "the tooling told me what's wrong" instead of "the
tooling is broken" -- this third bad state used to fall straight through to
git's raw message instead. The fix adds two read-only checks, in the same
voice as the existing refusals, BEFORE `git pull` ever runs:

  * `git rev-parse --abbrev-ref @{upstream}` fails when the branch was never
    tracked at all -- covered by
    test_no_upstream_configured_refuses_before_pull below.
  * `git ls-remote --heads origin <branch>` returns nothing when the branch
    WAS tracked but has since been deleted from the remote -- the actual
    measured case, covered by
    test_upstream_configured_but_remote_branch_deleted_refuses_before_pull.
    (`@{upstream}` alone does NOT catch this: a plain `git push -u` leaves a
    local remote-tracking ref that a later remote-side deletion does not
    prune, so it still resolves. `ls-remote` asks the remote directly.)

⛔ Neither check may auto-switch branches -- the operator chooses, the script
only refuses and explains. Confirmed below: no `git checkout` call appears
anywhere in the log for either scenario.

RULING B (HIGH). "Upgrade to main" never verified you were ON main -- a host
on a feature branch that still exists on the remote would pull it forward
and exit 0, reporting success while the release code never moved. The fix
states the branch plainly the moment it is known and repeats a loud,
non-fatal notice at the end of the run (mirroring the existing linger-
verdict pattern in test_update_framework_linger.py) so it survives to
wherever the operator actually looks, covered by
test_non_main_branch_notice_appears_early_and_at_closing_banner and
test_main_branch_produces_no_notice below.

Reuses the hermetic harness from test_update_framework_live_execution.py:
real script, PATH-stubbed git/uv/systemctl/curl/loginctl (`ls-remote`,
`rev-parse` and `symbolic-ref` alone delegate to the real git binary,
scoped to the sandbox's own throwaway .git and its local, file-path-only
"origin" remote -- see that file's _make_live_sandbox docstring). Nothing
real is ever touched: no real remote, no network, no live host.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from test_update_framework_live_execution import (  # noqa: E402
    _make_live_sandbox,
    _stub_path_env,
    _run_live,
    _strip_ansi,
)


def _checkout_new_branch(repo: Path, name: str) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", name], cwd=repo, check=True)


def _push_upstream(repo: Path, name: str) -> None:
    subprocess.run(["git", "push", "-q", "-u", "origin", name], cwd=repo, check=True)


def _delete_branch_on_remote(remote: Path, name: str) -> None:
    """Simulate a squash-merged PR: the branch's ref is removed from the
    bare 'remote' repo, but nothing here touches the sandbox's own local
    remote-tracking ref -- exactly what a real `git push -u` followed by a
    remote-side deletion leaves behind."""
    subprocess.run(
        ["git", "update-ref", "-d", f"refs/heads/{name}"], cwd=remote, check=True
    )


def _break_remote_transport(repo: Path) -> None:
    """Point 'origin' at a path that does not exist, so `git ls-remote`
    against it fails with a TRANSPORT-style error (verified locally: exit
    128) rather than the definitive "branch absent" exit 2 a reachable
    remote gives back for an unknown ref. Still hermetic -- a local path,
    never a real network location -- it fails because nothing is there,
    the same SHAPE of failure an offline or unreachable real remote
    produces, which is all RULING 2's distinction cares about."""
    subprocess.run(
        ["git", "remote", "set-url", "origin", "/no/such/path/does-not-exist.git"],
        cwd=repo, check=True,
    )


# ── RULING A ──────────────────────────────────────────────────────────────

def test_no_upstream_configured_refuses_before_pull(tmp_path):
    """A branch that was never pushed / never tracked at all: `@{upstream}`
    fails, and the script refuses with a clear, named message instead of
    letting `git pull` run at all."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "totally-local-branch")
    # Deliberately never pushed -- no upstream tracking configured.

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode != 0, out
    assert "has no upstream configured" in out, out
    assert "totally-local-branch" in out, out
    # The raw git error this refusal exists to pre-empt must never surface.
    assert "no such ref was fetched" not in out, out
    # And `git pull` itself must never have been invoked -- refused before it.
    assert not any(
        line.startswith("git ") and " pull " in f" {line} "
        for line in log_text.splitlines()
    ), f"'git pull' was invoked despite the missing-upstream refusal:\n{log_text}"
    assert "checkout" not in log_text, (
        f"the script must never auto-switch branches:\n{log_text}"
    )


def test_upstream_configured_but_remote_branch_deleted_refuses_before_pull(tmp_path):
    """The MEASURED case: the branch was tracked (pushed with -u) but its PR
    then merged and the remote deleted it (this repo squash-merges and
    deletes branches on merge). `@{upstream}` still resolves via the stale
    local remote-tracking ref; `ls-remote --heads origin <branch>` is what
    actually catches it."""
    repo, log_path = _make_live_sandbox(tmp_path)
    remote = tmp_path / "remote.git"
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "fix/some-merged-feature")
    _push_upstream(repo, "fix/some-merged-feature")
    _delete_branch_on_remote(remote, "fix/some-merged-feature")

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode != 0, out
    assert "no longer exists on origin" in out, out
    assert "fix/some-merged-feature" in out, out
    assert "squash-merges" in out, out
    # The raw git error this refusal exists to pre-empt must never surface.
    assert "no such ref was fetched" not in out, out
    assert not any(
        line.startswith("git ") and " pull " in f" {line} "
        for line in log_text.splitlines()
    ), f"'git pull' was invoked despite the deleted-remote-branch refusal:\n{log_text}"
    assert "checkout" not in log_text, (
        f"the script must never auto-switch branches:\n{log_text}"
    )


def test_branch_that_still_exists_on_remote_passes_the_guard(tmp_path):
    """Control: a tracked branch that DOES still exist on the remote must
    sail through the new guard and reach the real (stubbed) `git pull` --
    proving the guard targets the deleted/untracked cases specifically, not
    every non-main branch."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "still-alive-branch")
    _push_upstream(repo, "still-alive-branch")

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert any(
        line.startswith("git ") and "pull" in line for line in log_text.splitlines()
    ), f"'git pull' should have been reached and stub-logged:\n{log_text}"


# ── RULING B ──────────────────────────────────────────────────────────────

def test_non_main_branch_notice_appears_early_and_at_closing_banner(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "still-alive-branch")
    _push_upstream(repo, "still-alive-branch")

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out

    occurrences = [i for i, line in enumerate(out.splitlines())
                   if "not main" in line]
    assert occurrences, f"no 'not main' branch notice appeared at all:\n{out}"

    lines = out.splitlines()
    verified_idx = next(
        i for i, line in enumerate(lines) if "Update complete and VERIFIED" in line
    )
    assert any(i > verified_idx for i in occurrences), (
        f"the non-main branch notice appeared only early and did not survive "
        f"to the closing banner (VERIFIED at line {verified_idx}, notice "
        f"lines {occurrences}):\n{out}"
    )
    # Named explicitly, not just "not main" in the abstract.
    assert "still-alive-branch" in out, out


def test_main_branch_produces_no_notice(tmp_path):
    """Control: the default sandbox (branch main) must stay quiet -- the
    notice is for the non-main case specifically, never noise on every run."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert "not main" not in out, out


# ── RULING 1 (this branch): --dry-run must not DIE on the guard ─────────────
#
# The guard above runs before run()'s own DRY_RUN gate, so a plain die()
# inside it would make --dry-run exit non-zero on a branch with no upstream
# or an unreachable remote -- exactly backwards for a flag documented as
# "print, run nothing". Under --dry-run the guard must print the same
# refusal as an unmistakably PREDICTED outcome and let the rest of the dry
# run's step previews keep printing, exiting 0.

def test_dry_run_no_upstream_predicts_refusal_but_exits_zero_and_continues(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "totally-local-branch-dry-run")
    # Deliberately never pushed -- no upstream tracking configured, the
    # same real-refusal state test_no_upstream_configured_refuses_before_pull
    # covers for a REAL run.

    proc = _run_live(repo, env, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "has no upstream configured" in out, out
    assert "PREDICTED" in out, out
    assert "Dry run complete" in out, out
    # The dry run kept going past the guard: later steps still printed
    # their own previews rather than the run stopping dead at step 0.
    assert "backfill_domain_of.py" in out, (
        f"a predicted refusal under --dry-run must not truncate the rest "
        f"of the run's step previews:\n{out}"
    )
    # git pull must never actually run -- the guard still blocks the ONE
    # step it exists to block, it just doesn't kill the whole dry run.
    assert not any(
        line.startswith("git ") and " pull " in f" {line} "
        for line in log_text.splitlines()
    ), f"'git pull' was invoked despite the predicted refusal:\n{log_text}"


def test_dry_run_detached_head_predicts_refusal_but_exits_zero(tmp_path):
    """Same RULING 1 guarantee for the OTHER die() this guard used to have
    -- the detached-HEAD refusal, which fires before UPDATE_BRANCH is even
    set."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=repo, check=True)

    proc = _run_live(repo, env, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "DETACHED HEAD" in out, out
    assert "PREDICTED" in out, out
    assert "Dry run complete" in out, out


# ── RULING 2 (this branch): "no answer" is NOT "branch deleted" ─────────────
#
# git ls-remote failing to REACH the remote at all (offline, a proxy, a
# slow/unreachable remote) must never be read as the DEFINITIVE negative a
# reachable remote gives back for a genuinely absent branch (exit 2). The
# measured-case refusal test above
# (test_upstream_configured_but_remote_branch_deleted_refuses_before_pull)
# already proves the exit-2 case still refuses; this proves the OTHER exit
# codes do not.

def test_remote_unreachable_does_not_refuse_and_says_so(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "unreachable-remote-branch")
    _push_upstream(repo, "unreachable-remote-branch")
    _break_remote_transport(repo)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert "could not verify" in out.lower(), out
    # The definitive-negative refusal message must never fire for a merely
    # unreachable remote.
    assert "no longer exists on origin" not in out, out
    # And the run actually proceeded past the guard -- git pull was reached
    # (the stub logs it without ever touching the broken remote for real).
    assert any(
        line.startswith("git ") and "pull" in line for line in log_text.splitlines()
    ), f"'git pull' should have been reached despite the unreachable remote:\n{log_text}"


def test_remote_unreachable_under_dry_run_also_proceeds(tmp_path):
    """The warning is not a refusal, so it needs no RULING-1 dry-run
    carve-out of its own -- confirm --dry-run behaves the same way (reaches
    the dry-run banner, never treats the unreachable remote as fatal)."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    _checkout_new_branch(repo, "unreachable-remote-branch-dry")
    _push_upstream(repo, "unreachable-remote-branch-dry")
    _break_remote_transport(repo)

    proc = _run_live(repo, env, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Dry run complete" in out, out
    assert "could not verify" in out.lower(), out
    assert "no longer exists on origin" not in out, out
