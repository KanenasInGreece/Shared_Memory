"""update_framework.sh — the stack pin drift NOTICE (decision:1589 / fact:1588).

v0.9.55 moved the compose image pins (pgvector, neo4j) but update_framework.sh
does not, and by ruling never will, recreate containers on its own -- that
stays a standalone script (reconcile_stack.sh) the operator runs on their own
word. update_framework.sh's own obligation is narrower and easy to get wrong
in either direction: it must ALWAYS show the operator whether a reconcile is
now needed, and it must NEVER run the reconcile itself.

WHAT THIS FILE PINS, BY VALUE, not by "the script didn't crash":

  1. update_framework.sh calls reconcile_stack.sh with `--dry-run` ONLY --
     never a bare invocation that could recreate a container from inside an
     "update the framework" run.
  2. When that dry-run reports drift (exit 2), a clearly delimited
     "STACK UPDATE REQUIRED" block appears, carrying the drift table AND both
     commands (`--dry-run` first, then the bare reconcile) an operator needs.
  3. The words "stack reconcile REQUIRED" land on the run's FINAL status line
     ("Update complete and VERIFIED" / "Update finished, but UNVERIFIED") when
     drift is present, and are ABSENT from it when drift is not -- an operator
     reading only the last line of a long transcript must still see it.
  4. The notice reaches every terminal path a run can take: the success
     banner, the --dry-run banner, the AGENT_TOKEN-missing early exit, and the
     postflight-failure die() path -- the same "every terminal path" property
     already pinned for the linger verdict in test_update_framework_linger.py,
     which this file's harness and stub-reuse pattern deliberately mirrors.
  5. update_framework.sh's own exit status is NEVER changed by drift alone --
     a run that would otherwise succeed still exits 0 with drift present.

HOW THIS IS TESTED. Reuses the hermetic live-execution harness from
test_update_framework_live_execution.py (real update_framework.sh, PATH-
stubbed git/uv/systemctl/curl/loginctl, throwaway sandbox repo -- see that
file's module docstring for the full inventory). reconcile_stack.sh's OWN
correctness (the drift table, the image-tag/floating logic, the actual pull/
up/ALTER EXTENSION sequence) is exercised separately in
test_reconcile_stack.py; here it is replaced by a trivial stand-in at the
exact REPO_ROOT-relative path update_framework.sh invokes directly (the same
idiom the harness already uses for sync_skills.sh/postflight.sh), so these
tests are about the CALLING code's behaviour, not the callee's.
"""
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from test_update_framework_live_execution import (  # noqa: E402
    _make_live_sandbox,
    _stub_path_env,
    _run_live,
    _strip_ansi,
)

_DRIFT_TABLE = (
    "SERVICE                STATUS      PINNED                                 RUNNING\n"
    "postgres               DRIFT       pgvector/pgvector:0.8.6-pg17           pgvector/pgvector:0.8.5-pg17\n"
    "\n"
    "DRIFT in 1 row(s):\n"
    "  - postgres: pinned pgvector/pgvector:0.8.6-pg17, running pgvector/pgvector:0.8.5-pg17\n"
)


def _write_reconcile_stub(repo: Path, log_path: Path, rc: int) -> None:
    """Overwrite the reconcile_stack.sh stand-in _make_live_sandbox() does
    NOT ship by default (it predates this script) at the exact
    REPO_ROOT-relative path update_framework.sh invokes directly. rc=2
    reports drift (with a canned table on stdout); rc=0 reports none."""
    path = repo / "shared-memory" / "scripts" / "reconcile_stack.sh"
    if rc == 2:
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "reconcile_stack.sh $*" >> "{log_path}"\n'
            f"cat <<'DRIFTEOF'\n{_DRIFT_TABLE}DRIFTEOF\n"
            "exit 2\n"
        )
    else:
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "reconcile_stack.sh $*" >> "{log_path}"\n'
            'echo "No drift — every deployed pin matches its running container."\n'
            "exit 0\n"
        )
    path.write_text(body)
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_postflight_stub(repo: Path, log_path: Path, rc: int) -> None:
    postflight = repo / "shared-memory" / "scripts" / "postflight.sh"
    postflight.write_text(
        f'#!/usr/bin/env bash\necho "postflight.sh $*" >> "{log_path}"\nexit {rc}\n'
    )
    st = postflight.stat()
    postflight.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _reconcile_invocations(log_text: str):
    return [line for line in log_text.splitlines()
            if line.startswith("reconcile_stack.sh ")]


# ── The drift check is called read-only, ONLY with --dry-run ────────────────

def test_reconcile_stack_is_called_with_dry_run_and_never_bare(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)

    proc = _run_live(repo, env, "--skip-backup")
    log_text = log_path.read_text()

    assert proc.returncode == 0, _strip_ansi(proc.stdout + proc.stderr)
    invocations = _reconcile_invocations(log_text)
    assert invocations, f"reconcile_stack.sh was never invoked:\n{log_text}"
    assert all("--dry-run" in line for line in invocations), (
        f"reconcile_stack.sh was invoked WITHOUT --dry-run -- update_framework.sh "
        f"must never run the reconcile itself:\n{invocations}"
    )
    # And exactly once per run -- never a second, bare call.
    assert len(invocations) == 1, invocations


# ── No drift: quiet, no REQUIRED wording anywhere ────────────────────────────

def test_no_drift_final_line_has_no_required_wording(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=0)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert "stack reconcile REQUIRED" not in out, out
    assert "STACK UPDATE REQUIRED" not in out, out


# ── Drift: the delimited block, the table, both commands, the final line ────

def test_drift_final_line_carries_required_wording(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out  # drift alone must never fail the update
    assert "Update complete and VERIFIED" in out, out
    trailer = out.rsplit("Update complete and VERIFIED", 1)[1]
    # The FINAL status line itself (the same physical line the banner prints
    # on) must carry the wording -- not merely somewhere later in the output.
    final_line = next(
        line for line in out.splitlines() if "Update complete and VERIFIED" in line
    )
    assert "stack reconcile REQUIRED" in final_line, (
        f"the final status line did not carry 'stack reconcile REQUIRED':\n{final_line!r}"
    )
    assert "STACK UPDATE REQUIRED" in trailer, (
        f"the delimited drift block never appeared after the final line:\n{out}"
    )
    assert "postgres" in trailer and "0.8.6-pg17" in trailer, (
        f"the drift table itself did not reach the operator:\n{out}"
    )
    assert "reconcile_stack.sh --dry-run" in trailer, out
    # The bare (reconciling) command must be shown too, distinct from the
    # --dry-run one just asserted above.
    assert "bash shared-memory/scripts/reconcile_stack.sh\n" in trailer or \
           trailer.rstrip().endswith("bash shared-memory/scripts/reconcile_stack.sh"), (
        f"the bare reconcile command was not shown:\n{trailer}"
    )


def test_drift_notice_never_runs_the_reconcile_itself(tmp_path):
    """The single most important guard in this file: however drift is
    reported, update_framework.sh must never itself invoke reconcile_stack.sh
    without --dry-run -- that would recreate database containers from inside
    an unrelated 'update the framework' run."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)

    proc = _run_live(repo, env, "--skip-backup")
    log_text = log_path.read_text()

    assert proc.returncode == 0, _strip_ansi(proc.stdout + proc.stderr)
    bare_calls = [
        line for line in _reconcile_invocations(log_text)
        if "--dry-run" not in line
    ]
    assert not bare_calls, f"reconcile_stack.sh was invoked WITHOUT --dry-run: {bare_calls}"


# ── update_framework.sh's own --dry-run mode still runs the (read-only)
#    check and still reports it ──────────────────────────────────────────────

def test_update_dry_run_still_reports_drift(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)

    proc = _run_live(repo, env, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Dry run complete" in out, out
    assert "STACK UPDATE REQUIRED" in out, out


def test_update_dry_run_quiet_when_no_drift(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=0)

    proc = _run_live(repo, env, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Dry run complete" in out, out
    assert "STACK UPDATE REQUIRED" not in out, out


# ── Every terminal path reports it: AGENT_TOKEN-missing early exit ──────────

def test_agent_token_missing_early_exit_carries_the_notice(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)
    env.pop("AGENT_TOKEN", None)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "UNVERIFIED" in out, out
    final_line = next(
        line for line in out.splitlines() if "Update finished, but UNVERIFIED" in line
    )
    assert "stack reconcile REQUIRED" in final_line, final_line
    assert "STACK UPDATE REQUIRED" in out, out


def test_agent_token_missing_early_exit_quiet_when_no_drift(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=0)
    env.pop("AGENT_TOKEN", None)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    final_line = next(
        line for line in out.splitlines() if "Update finished, but UNVERIFIED" in line
    )
    assert "stack reconcile REQUIRED" not in final_line, final_line
    assert "STACK UPDATE REQUIRED" not in out, out


# ── Every terminal path reports it: postflight-failure die() path ───────────

def test_postflight_failure_die_path_carries_the_notice(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)
    _write_postflight_stub(repo, log_path, rc=1)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "postflight FAILED" in out, out
    assert "STACK UPDATE REQUIRED" in out, out


def test_postflight_failure_die_path_quiet_when_no_drift(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=0)
    _write_postflight_stub(repo, log_path, rc=1)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "postflight FAILED" in out, out
    assert "STACK UPDATE REQUIRED" not in out, out


# ── A drift verdict never changes the update's own exit status ──────────────

def test_drift_does_not_change_success_exit_status(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env_drift = _stub_path_env(tmp_path, log_path)
    _write_reconcile_stub(repo, log_path, rc=2)
    proc_drift = _run_live(repo, env_drift, "--skip-backup")

    repo2, log_path2 = _make_live_sandbox(tmp_path / "second")
    env_clean = _stub_path_env(tmp_path / "second", log_path2)
    _write_reconcile_stub(repo2, log_path2, rc=0)
    proc_clean = _run_live(repo2, env_clean, "--skip-backup")

    assert proc_drift.returncode == proc_clean.returncode == 0


# ── An unreachable/failing reconcile_stack.sh must never break the update ───

def test_reconcile_stack_unexpected_failure_is_treated_as_unknown_not_fatal(tmp_path):
    """rc=1 (a genuine script error, not the documented 0/2) must not be
    misread as either verdict and must never abort the update."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    path = repo / "shared-memory" / "scripts" / "reconcile_stack.sh"
    path.write_text(
        f'#!/usr/bin/env bash\necho "reconcile_stack.sh $*" >> "{log_path}"\n'
        f'echo "boom" >&2\nexit 1\n'
    )
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert "stack reconcile REQUIRED" not in out, out
    assert "STACK UPDATE REQUIRED" not in out, out
