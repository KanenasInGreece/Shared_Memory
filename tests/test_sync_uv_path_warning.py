"""sync_skills.sh half of the uv-PATH-reachability warning (I-P1 / I-P3).

See tests/test_preflight_uv_path_check.py for the full incident and
rationale — `preflight.sh` can only say "uv exists somewhere on this host";
it has no idea whether any agent skill is actually deployed. This script is
the one place that DOES know, because it is about to write (or has already
written) real skill installs into real directories — so the same warning
belongs here too, reaching the operator on every release rather than only
when someone happens to re-run preflight.

The mechanism mirrors preflight.sh's exactly: `env -i` clears the whole
environment so no inherited PATH edit survives, and `getconf PATH` is the
platform's own compiled-in default — the closest thing to "what a
profile-free shell starts with" any POSIX host can answer, depending on
neither uv nor python.

Two behaviours specific to THIS script are pinned here and nowhere else:
  * it only warns when at least one agent install actually EXISTS on disk
    (sync is the one place that knows that) — no install, no warning;
  * it warns ONCE per run, never once per directory — the cause is a
    property of the host's PATH, not of any individual agent's install.

⚠ EXECUTABLE: drives the real script against a temporary tree via
SHARED_MEMORY_SYNC_AGENTS, the same seam tests/test_skill_delivery.py uses.
Phase 1 (source → tracked skill copy) is skipped
(SHARED_MEMORY_SYNC_SKIP_TRACKED=1) so a test run cannot silently repair the
repo's own tracked copy — same reason that test suite skips it.
"""
import os
import stat
import subprocess

_REPO = os.path.join(os.path.dirname(__file__), "..")
_SYNC = os.path.join(_REPO, "shared-memory", "scripts", "sync_skills.sh")

_WARN_MARKER = "resolves ONLY when your shell profile is loaded"


def _make_stub(dir_path, name, body):
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _stub_path(tmp_path, sys_path_answer, uv_broken=False):
    agentbin = tmp_path / "agentbin"
    binstub = tmp_path / "binstub"
    uv_body = ("#!/usr/bin/env bash\nexit 1\n" if uv_broken else
               "#!/usr/bin/env bash\necho 'uv 0.1.0 (test stub)'\n")
    _make_stub(str(agentbin), "uv", uv_body)
    _make_stub(str(binstub), "getconf",
              f'#!/usr/bin/env bash\necho "{sys_path_answer}"\n')
    return f"{agentbin}{os.pathsep}{binstub}{os.pathsep}{os.environ.get('PATH', '')}"


def _run_sync(agent_dirs, extra_path):
    env = dict(os.environ)
    env["SHARED_MEMORY_SYNC_AGENTS"] = ":".join(agent_dirs)
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
    env["PATH"] = extra_path
    return subprocess.run(["bash", _SYNC], capture_output=True, text=True,
                          env=env, cwd=_REPO, timeout=180)


# ── I-P1: warns when an EXISTING install cannot reach uv without a profile ──

def test_warns_once_when_an_existing_install_cannot_reach_uv_without_a_profile(tmp_path):
    install = tmp_path / "existing" / "shared-memory"
    install.mkdir(parents=True)
    path = _stub_path(tmp_path, sys_path_answer="/nonexistent-test-sysdir-sync1")
    result = _run_sync([str(install)], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(_WARN_MARKER) == 1, (
        f"expected the warning exactly once:\n{result.stdout}"
    )


def test_no_warning_when_no_install_exists_yet(tmp_path):
    """Sync is the one place that KNOWS an install is really there — a target
    that is not installed yet (SKIP path) has nothing here to warn about."""
    absent = tmp_path / "not-installed" / "shared-memory"
    path = _stub_path(tmp_path, sys_path_answer="/nonexistent-test-sysdir-sync2")
    result = _run_sync([str(absent)], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _WARN_MARKER not in result.stdout


def test_warning_appears_once_not_once_per_directory(tmp_path):
    """Two existing installs must still produce exactly ONE warning — the
    cause is a property of the host's PATH, not of any one agent's
    directory, and repeating it per target would just be noise."""
    install_a = tmp_path / "agent-a" / "shared-memory"
    install_b = tmp_path / "agent-b" / "shared-memory"
    install_a.mkdir(parents=True)
    install_b.mkdir(parents=True)
    path = _stub_path(tmp_path, sys_path_answer="/nonexistent-test-sysdir-sync3")
    result = _run_sync([str(install_a), str(install_b)], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(_WARN_MARKER) == 1, (
        f"expected exactly one warning across two directories, got:\n{result.stdout}"
    )


def test_sync_stays_non_fatal_and_still_delivers_when_uv_is_profile_only(tmp_path):
    """Sync's job is delivery — the warning must never turn into a failure,
    and delivery itself must still succeed alongside it."""
    install = tmp_path / "existing" / "shared-memory"
    install.mkdir(parents=True)
    path = _stub_path(tmp_path, sys_path_answer="/nonexistent-test-sysdir-sync4")
    result = _run_sync([str(install)], path)
    assert result.returncode == 0
    assert (install / "SKILL.md").is_file(), "delivery itself must still succeed"


# ── I-P3: no false alarm on a correctly-set-up host ─────────────────────────

def test_no_false_alarm_when_uv_is_on_the_system_default_path(tmp_path):
    install = tmp_path / "existing" / "shared-memory"
    install.mkdir(parents=True)
    agentbin = tmp_path / "agentbin"
    path = _stub_path(tmp_path, sys_path_answer=f"{agentbin}:/usr/bin:/bin")
    result = _run_sync([str(install)], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _WARN_MARKER not in result.stdout


# ── I-P2: the check's answer never depends on uv actually running ──────────

def test_the_check_answers_correctly_even_when_uv_itself_is_non_functional(tmp_path):
    """Same proof as the preflight half: the stub `uv` exits 1 the moment it
    is executed, so a correct WARN here cannot be coming from running uv —
    only `command -v` (existence on PATH) is available to have produced it."""
    install = tmp_path / "existing" / "shared-memory"
    install.mkdir(parents=True)
    path = _stub_path(tmp_path, sys_path_answer="/nonexistent-test-sysdir-sync5",
                      uv_broken=True)
    result = _run_sync([str(install)], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _WARN_MARKER in result.stdout
