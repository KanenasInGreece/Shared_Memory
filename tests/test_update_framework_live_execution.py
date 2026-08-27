"""update_framework.sh — the LIVE (non-`--dry-run`) execution path of the
domain-backfill opt-in (v0.9.69, fact:1734 C(d) / decision:1736 / O1),
carrying forward the review finding TV-1/TV-2 in
Local_Documentation/Reviews/Test_Verification_Review.md.

RE-RULED (v0.9.69): the backfill used to run by default and
`--no-domain-backfill` opted OUT; it now runs only when `--domain-backfill`
opts IN, and `--no-domain-backfill` is a one-release no-op. The gap TV-1/TV-2
found is unchanged in shape, just flipped in polarity: it is still the LIVE
`else` branch, not the dry-run preview, that actually decides whether
backfill_domain_of.py --apply is invoked for real.

WHY THIS EXISTS. Every test in test_update_framework_no_domain_backfill.py
passes `--dry-run`. Under `--dry-run`, run()/run_soft() print a step's
command line and return WITHOUT ever calling it -- so those tests only ever
exercise the SKIP branch and the DRY-RUN PREVIEW branch of the
`if DOMAIN_BACKFILL != 1 ... elif DRY_RUN ... else` selection in
update_framework.sh. The `else` branch -- the one that actually calls
backfill_domain_of.py --apply for real -- is never taken by any test in this
suite. A mutation that re-scopes the DOMAIN_BACKFILL check so it is only
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
    without ever touching real infrastructure. Returns (repo, log_path).

    The repo is given a real branch (deterministically named "main" --
    pinned via `symbolic-ref HEAD` rather than trusting whatever
    init.defaultBranch this workstation happens to have), one commit, and a
    real local "remote" -- a bare repo at tmp_path/remote.git -- with
    upstream tracking configured via `git push -u`. This is what a real
    upgrade host looks like (a branch that pulls from a configured, live
    remote), and it is what test_update_framework_branch_guard.py's Ruling-A
    tests mutate (deleting a branch from remote.git to reproduce "PR merged,
    remote branch deleted") and Ruling-B tests build on (checking out and
    pushing a second, non-main branch that still exists on the remote). It
    also keeps the DEFAULT sandbox (branch main, tracked, present on the
    remote) passing cleanly through the new pre-pull guard in step 0, so
    every test in THIS file that doesn't care about branch state is
    unaffected by it."""
    repo = tmp_path / "repo"
    scripts_dir = repo / "shared-memory" / "scripts"
    scripts_dir.mkdir(parents=True)
    (repo / "shared-memory" / ".env").write_text("DUMMY=1\n")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    # Pin the initial branch name explicitly -- do not trust this
    # workstation's own init.defaultBranch (older git defaults to "master").
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                    cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "sandbox@example.invalid"],
                    cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "sandbox"],
                    cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    # A real local "remote" -- a bare repo, never a network location -- so
    # `git ls-remote --heads origin <branch>` (RULING A's primary
    # instrument) and `git rev-parse --abbrev-ref @{upstream}` behave exactly
    # like they would against a real GitHub remote, entirely offline.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)],
                    cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                    cwd=repo, check=True)

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

    # "symbolic-ref", "rev-parse" and "ls-remote" are delegated to the REAL
    # git -- all three are read-only and scoped to the sandbox's own
    # throwaway .git plus its local (file-path, never network) "origin"
    # remote, so branch resolution and the RULING-A pre-pull guard (which
    # calls `rev-parse --abbrev-ref @{upstream}` and
    # `ls-remote --heads origin <branch>`) behave exactly like a real
    # checkout. None of the three is ever $1 -- the script calls
    # `git -C "$REPO_ROOT" <subcommand> ...`, so the match has to scan all
    # args, not just the first. Anything else (`pull --ff-only`) is logged
    # and reports success without touching any remote -- `pull` is
    # deliberately NOT in this list, so the actual fetch/merge stays fully
    # stubbed regardless of the sandbox's real git state.
    _make_exec(
        stub_dir / "git",
        f'#!/usr/bin/env bash\n'
        f'echo "git $*" >> "{log_path}"\n'
        f'for arg in "$@"; do\n'
        f'    case "$arg" in\n'
        f'        symbolic-ref|rev-parse|ls-remote)\n'
        f'            exec "{real_git}" "$@"\n'
        f'            ;;\n'
        f'    esac\n'
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
    # The linger-check step (U1) calls `loginctl show-user ... --property=
    # Linger`. Default this stub to a quiet "yes" so the tests in THIS file
    # (which do not care about linger) stay deterministic regardless of the
    # real host's own linger state, rather than falling through to a real
    # `loginctl` on PATH. test_update_framework_linger.py overwrites this
    # exact file with other verdicts for its own scenarios.
    _make_exec(
        stub_dir / "loginctl",
        f'#!/usr/bin/env bash\n'
        f'echo "loginctl $*" >> "{log_path}"\n'
        f'case "$1" in\n'
        f'    show-user) echo "Linger=yes"; exit 0 ;;\n'
        f'    *) exit 0 ;;\n'
        f'esac\n',
    )

    env = dict(os.environ)
    # Never let a real GATEWAY_URL/GATEWAY_UNIT/GATEWAY_RESTART_CMD exported
    # in the harness's OWN environment leak into the sandboxed run. The
    # script honours all three from the environment and runs
    # `bash -c "$GATEWAY_RESTART_CMD"` unconditionally in step 5 -- a
    # developer or CI runner with GATEWAY_RESTART_CMD set to something that
    # touches a real service (systemctl is stubbed on PATH here, but e.g.
    # `sudo systemctl restart ...` is not) would have this suite act on the
    # REAL production gateway. Popping them forces every run to fall back to
    # the script's own defaults, which this harness's stubs are built to
    # answer to.
    for _leaky in ("GATEWAY_URL", "GATEWAY_UNIT", "GATEWAY_RESTART_CMD"):
        env.pop(_leaky, None)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    # check_linger()'s PRIMARY instrument is /var/lib/systemd/linger/<user> --
    # a real filesystem path, not something PATH-stubbing can intercept.
    # RULING 3 (this branch) removed the $LINGER_DIR environment seam that
    # used to point this at a sandbox path the tests controlled -- it was a
    # live-environment backdoor (export LINGER_DIR=/tmp silently bypassed
    # the whole check on a REAL run), so check_linger() now only accepts the
    # directory as a function parameter, and production's one call site
    # passes none. A full end-to-end run of the real script therefore always
    # consults the REAL /var/lib/systemd/linger on whatever host runs this
    # suite -- none of the tests in THIS file assert on the linger verdict
    # text, so this is harmless here regardless of what it reads; the tests
    # that DO care about a specific verdict (test_update_framework_linger.py)
    # control it via $USER instead -- see that file for why.
    # Skip the AGENT_TOKEN-not-exported early exit (unrelated to step
    # numbering -- it is a precondition on step 8, "prove it") so the run
    # reaches (stubbed) postflight and completes with rc=0 — proving later
    # steps fire, not just that the script stops partway through.
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

def test_live_run_with_domain_backfill_flag_invokes_backfill_with_apply(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    proc = _run_live(repo, env, "--skip-backup", "--domain-backfill")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    invocations = _backfill_invocations(log_text)
    assert invocations, (
        f"--domain-backfill on a LIVE run never invoked backfill_domain_of.py "
        f"via uv:\nlog:\n{log_text}\nstdout:\n{out}"
    )
    assert any("--apply" in line for line in invocations), (
        f"backfill_domain_of.py was invoked but WITHOUT --apply:\n{invocations}"
    )
    # Later steps still fire — the run is not truncated at step 6.
    assert "sync_skills.sh" in log_text, "step 7 (sync_skills.sh) never ran"
    assert "postflight.sh" in log_text, "step 8 (postflight.sh) never ran"
    assert "Update complete and VERIFIED" in out, out


def test_live_run_by_default_never_invokes_backfill(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    invocations = _backfill_invocations(log_text)
    assert not invocations, (
        f"the default (no --domain-backfill) LIVE run invoked "
        f"backfill_domain_of.py anyway via uv:\n{invocations}\n"
        f"full log:\n{log_text}"
    )
    assert "SKIPPED — opt-in as of this release" in out, out
    # Later steps still fire even when this one is skipped.
    assert "sync_skills.sh" in log_text, "step 7 (sync_skills.sh) never ran"
    assert "postflight.sh" in log_text, "step 8 (postflight.sh) never ran"
    assert "Update complete and VERIFIED" in out, out


def test_live_run_with_no_domain_backfill_flag_alone_still_never_invokes_backfill(tmp_path):
    """--no-domain-backfill is a one-release no-op now that the default is
    already skip -- a caller still passing it (an un-updated wrapper script)
    must not have that flag start invoking the backfill by some fallthrough,
    and the deprecation notice must print."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    proc = _run_live(repo, env, "--skip-backup", "--no-domain-backfill")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    invocations = _backfill_invocations(log_text)
    assert not invocations, (
        f"--no-domain-backfill on a LIVE run invoked backfill_domain_of.py "
        f"anyway via uv:\n{invocations}\nfull log:\n{log_text}"
    )
    assert "Notice: --no-domain-backfill" in out, out
    assert "SKIPPED — opt-in as of this release" in out, out


# ── Ruling 3: a real GATEWAY_URL/GATEWAY_UNIT/GATEWAY_RESTART_CMD exported in
#    the environment this suite runs under must NEVER reach the sandboxed
#    script. `systemctl` is stubbed on PATH here, but a restart command like
#    `sudo systemctl restart hive-mind-gateway.service` runs `sudo` for real
#    -- a leaked value would have this suite act on a REAL production unit. ──

def test_leaky_gateway_env_vars_do_not_reach_the_sandboxed_run(tmp_path, monkeypatch):
    """A caller (developer shell, CI runner) with GATEWAY_RESTART_CMD/
    GATEWAY_URL/GATEWAY_UNIT exported for their OWN real deployment -- e.g. a
    privileged `sudo systemctl restart ...` -- must not have those values
    survive into the harness's env. monkeypatch sets them in THIS process's
    os.environ (restored automatically at teardown), exactly mirroring a
    real caller shell; _stub_path_env() builds `env = dict(os.environ)`
    internally and must pop all three before anything reaches the
    subprocess."""
    monkeypatch.setenv(
        "GATEWAY_RESTART_CMD", "sudo systemctl restart hive-mind-gateway.service"
    )
    monkeypatch.setenv("GATEWAY_URL", "http://production-host:8888")
    monkeypatch.setenv("GATEWAY_UNIT", "hive-mind-gateway.service")

    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)

    # _stub_path_env() must have stripped these BEFORE they ever reach the
    # subprocess -- assert on the env dict actually handed to _run_live().
    assert "GATEWAY_RESTART_CMD" not in env, (
        "GATEWAY_RESTART_CMD leaked into the sandboxed run's environment -- "
        "a real 'sudo systemctl restart ...' would execute for real"
    )
    assert "GATEWAY_URL" not in env
    assert "GATEWAY_UNIT" not in env

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    # The stubbed systemctl ran (via the script's own default restart
    # command against the stub GATEWAY_UNIT default), never a real `sudo`.
    assert "sudo" not in log_text, (
        f"a 'sudo'-prefixed restart command reached the log -- the leaked "
        f"env var was not actually stripped:\n{log_text}"
    )
