"""update_framework.sh — the LIVE (non-`--dry-run`) execution path of
`--no-domain-backfill` (branch fix/domain-backfill-opt-out, review finding
TV-1/TV-2 in Local_Documentation/Reviews/Test_Verification_Review.md).

WHY THIS EXISTS. Every test in test_update_framework_no_domain_backfill.py
passes `--dry-run`. Under `--dry-run`, run()/run_soft() print a step's
command line and return WITHOUT ever calling it -- so those tests only ever
exercise the SKIP branch and the DRY-RUN PREVIEW branch of the
`if NO_DOMAIN_BACKFILL ... elif DRY_RUN ... else` selection in
update_framework.sh. The `else` branch -- the one that actually calls
backfill_domain_of.py --apply for real -- is never taken by any test in this
suite. A mutation that re-scopes the NO_DOMAIN_BACKFILL check so it is only
ever consulted inside the DRY_RUN branch (i.e. the flag becomes a no-op for
every real, non-dry-run invocation) would leave that whole suite green.

This file closes that gap by running the REAL shipped script with DRY_RUN=0
end to end, inside a throwaway sandbox, with every external command it
would invoke replaced by a PATH stub that RECORDS its invocation (argv) to
a log file instead of performing it. Nothing real is ever touched: no git
remote, no Postgres, no Neo4j, no gateway, no network.

Commands traced through the script and why each is stubbed:
  * `git`     -- step 0 (git -C REPO_ROOT symbolic-ref, then git pull
                 --ff-only). The stub delegates `symbolic-ref` to the REAL
                 git binary (read-only, scoped to the sandbox's own throwaway
                 .git) so branch resolution behaves like a real checkout;
                 every other subcommand (`pull`) is just logged and reports
                 success without touching any remote.
  * `uv`      -- steps 2/3/4 (Postgres/Neo4j/project-identity migrations,
                 all run_soft -- non-fatal) and step 6, the domain backfill
                 itself (run -- fatal on failure). This is the command the
                 assertions below actually key on: whether its logged argv
                 contains `backfill_domain_of.py ... --apply`.
  * `systemctl` -- step 5's gateway restart (GATEWAY_RESTART_CMD's shipped
                 default). Logged and reports success without touching any
                 real unit.
  * `curl`    -- step 5's post-restart health wait and version comparison.
                 The stub always answers 200 with a canned
                 {"status":"ok","version":...} body whose version is made to
                 match the sandbox's own dummy coordinator.py, so the
                 script's own old-gateway guard (comparing the two) passes
                 without any real gateway.
  * `sync_skills.sh` / `postflight.sh` -- steps 7/8. Not PATH-stubbed (the
    script invokes them by explicit REPO_ROOT-relative path, not by PATH
    lookup) -- instead the sandbox ships trivial stand-in scripts at those
    exact paths that log their invocation and exit 0. These are throwaway
    files inside tmp_path, never the real shared-memory/scripts/*.sh.

`bash` itself, and everything else the script uses only for its own control
flow (sed, awk, printf, seq, mkdir, cat, dirname, pwd, command -v, curl's
absence check), is left to resolve on the real PATH -- only the five
programs above are intercepted.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

_REPO = Path(__file__).parent.parent
_REAL_SCRIPT = _REPO / "shared-memory" / "scripts" / "update_framework.sh"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_DUMMY_VERSION = "9.9.9-test"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_live_sandbox(tmp_path: Path):
    """A throwaway repo root wired up so a REAL (non-dry-run) run of
    update_framework.sh can reach its own final "postflight passed" line
    without ever touching real infrastructure. Returns (repo, log_path)."""
    repo = tmp_path / "repo"
    scripts_dir = repo / "shared-memory" / "scripts"
    scripts_dir.mkdir(parents=True)
    (repo / "shared-memory" / ".env").write_text("DUMMY=1\n")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    link = scripts_dir / "update_framework.sh"
    link.symlink_to(_REAL_SCRIPT.resolve())

    # A dummy coordinator.py — only FRAMEWORK_VERSION is read (via sed) by
    # the script's post-restart old-gateway guard. Never the real one.
    (scripts_dir / "coordinator.py").write_text(
        f'FRAMEWORK_VERSION = "{_DUMMY_VERSION}"\n'
    )

    log_path = tmp_path / "invocations.log"
    log_path.write_text("")

    # Throwaway stand-ins at the exact REPO_ROOT-relative paths the script
    # invokes directly (not via PATH) for steps 7/8.
    _make_exec(
        scripts_dir / "sync_skills.sh",
        f'#!/usr/bin/env bash\necho "sync_skills.sh $*" >> "{log_path}"\nexit 0\n',
    )
    _make_exec(
        scripts_dir / "postflight.sh",
        f'#!/usr/bin/env bash\necho "postflight.sh $*" >> "{log_path}"\nexit 0\n',
    )

    return repo, log_path


def _stub_path_env(tmp_path: Path, log_path: Path) -> dict:
    """Build a stub bin dir with fake git/uv/systemctl/curl that RECORD
    their invocation to log_path instead of performing it, and return an
    env with PATH prefixed by it (real PATH kept after, so bash/sed/awk/
    etc. still resolve normally)."""
    real_git = subprocess.run(
        ["which", "git"], capture_output=True, text=True, check=True
    ).stdout.strip()

    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(exist_ok=True)

    # "symbolic-ref" is delegated to the REAL git (read-only, scoped to the
    # sandbox's own throwaway .git) so branch resolution behaves like a real
    # checkout. It is never $1 -- the script calls `git -C "$REPO_ROOT"
    # symbolic-ref ...`, so the match has to scan all args, not just the
    # first. Anything else (`pull --ff-only`) is logged and reports success
    # without touching any remote.
    _make_exec(
        stub_dir / "git",
        f'#!/usr/bin/env bash\n'
        f'echo "git $*" >> "{log_path}"\n'
        f'for arg in "$@"; do\n'
        f'    if [[ "$arg" == "symbolic-ref" ]]; then\n'
        f'        exec "{real_git}" "$@"\n'
        f'    fi\n'
        f'done\n'
        f'exit 0\n',
    )
    _make_exec(
        stub_dir / "uv",
        f'#!/usr/bin/env bash\necho "uv $*" >> "{log_path}"\nexit 0\n',
    )
    _make_exec(
        stub_dir / "systemctl",
        f'#!/usr/bin/env bash\necho "systemctl $*" >> "{log_path}"\nexit 0\n',
    )
    _make_exec(
        stub_dir / "curl",
        f'#!/usr/bin/env bash\n'
        f'echo "curl $*" >> "{log_path}"\n'
        f'echo \'{{"status":"ok","version":"{_DUMMY_VERSION}"}}\'\n'
        f'exit 0\n',
    )

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    # Skip step 8's early "AGENT_TOKEN not exported" exit so the run reaches
    # (stubbed) postflight and completes with rc=0 — proving later steps
    # fire, not just that the script stops partway through.
    env["AGENT_TOKEN"] = "test-token"
    return env


def _run_live(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo / "shared-memory" / "scripts" / "update_framework.sh"), *args],
        capture_output=True, text=True, timeout=60, cwd=repo, env=env,
    )


def _backfill_invocations(log_text: str):
    """uv invocation lines that name backfill_domain_of.py."""
    return [line for line in log_text.splitlines()
            if line.startswith("uv ") and "backfill_domain_of.py" in line]


# ── TV-1 / TV-2: the live `else` branch IS exercised, and it is the ONLY
#    thing that decides whether backfill_domain_of.py --apply is invoked ──

def test_live_run_without_flag_invokes_backfill_with_apply(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    invocations = _backfill_invocations(log_text)
    assert invocations, (
        f"the default (flag absent) LIVE run never invoked backfill_domain_of.py "
        f"via uv:\nlog:\n{log_text}\nstdout:\n{out}"
    )
    assert any("--apply" in line for line in invocations), (
        f"backfill_domain_of.py was invoked but WITHOUT --apply:\n{invocations}"
    )
    # Later steps still fire — the run is not truncated at step 6.
    assert "sync_skills.sh" in log_text, "step 7 (sync_skills.sh) never ran"
    assert "postflight.sh" in log_text, "step 8 (postflight.sh) never ran"
    assert "Update complete and VERIFIED" in out, out


def test_live_run_with_flag_never_invokes_backfill(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    proc = _run_live(repo, env, "--skip-backup", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    invocations = _backfill_invocations(log_text)
    assert not invocations, (
        f"--no-domain-backfill was given on a LIVE (non-dry-run) run, but "
        f"backfill_domain_of.py was invoked anyway via uv:\n{invocations}\n"
        f"full log:\n{log_text}"
    )
    assert "SKIPPED — --no-domain-backfill given" in out, out
    # Later steps still fire even when this one is skipped.
    assert "sync_skills.sh" in log_text, "step 7 (sync_skills.sh) never ran"
    assert "postflight.sh" in log_text, "step 8 (postflight.sh) never ran"
    assert "Update complete and VERIFIED" in out, out
