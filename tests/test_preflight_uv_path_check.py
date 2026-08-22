"""Invariants I-P1 / I-P2 / I-P3 — preflight.sh detects an agent-unreachable uv.

WHY THIS EXISTS. `preflight.sh` already checked uv with `command -v uv`, and
that check is evaluated in whatever shell the OPERATOR ran preflight from —
almost always an interactive login shell that has sourced ~/.bashrc et al.
Measured on a real host: that check reported uv present and preflight
PASSED, on the exact machine where every agent invocation of the skill
failed, because every agent instead spawns uv through a non-interactive,
non-login shell — a CLI harness execs a command, it does not open a
terminal — which reads none of those profile files and starts with whatever
PATH its own parent process handed it.

This is not a misconfiguration: it is the expected outcome of the install
this project itself recommends. The upstream installer
(curl -LsSf https://astral.sh/uv/install.sh | sh) places uv under
$HOME/.local/bin and edits the operator's shell profile to expose it — the
skill's own script chain (`memory_bridge.py doctor`, invoked THROUGH `uv
run`) is structurally incapable of diagnosing this, because when uv is
missing, that chain never executes in the first place. So the check has to
live at the shell level, in a script that does not itself depend on uv.

The existing operator-facing check is UNCHANGED and stays a hard failure — an
operator who genuinely lacks uv must still fail preflight. This file pins the
SECOND, additive check: whether uv also resolves with NO shell profile in
effect. `env -i` clears the whole environment (not just PATH), so nothing
inherited can smuggle a profile's PATH edit back in; the reference PATH is
`getconf PATH`, the platform's own compiled-in default — the closest thing
any POSIX host can answer to "what does a shell have before anything
user-specific runs". This cannot know any particular AGENT's own PATH (a
framework may set its own), so it is worded as what it measured, never as a
verdict on a specific agent.

⚠ EXECUTABLE, not source-reading: every test drives the real script via
subprocess and controls only PATH — see tests/test_skill_delivery.py for the
established pattern this follows. Two fake binaries do the controlling:

  * a stub `uv` — makes the OPERATOR-level `command -v uv` succeed without
    depending on whatever uv build (or absence of one) this host happens to
    have;
  * a stub `getconf` — makes `getconf PATH` (the check's own reference for
    "the system default PATH") return a value the test picks, rather than
    whatever this real host's compiled-in default happens to be. The script
    genuinely calls getconf; the test overrides which directory resolves
    that name first on PATH — the same kind of seam
    SHARED_MEMORY_SYNC_AGENTS gives test_skill_delivery.py.

No DB, no Neo4j, nothing outside tmp_path is read or written. docker/git/etc.
still resolve normally through the real PATH appended after the stubs, so
the rest of preflight.sh runs exactly as it would for a real operator.
"""
import os
import stat
import subprocess

_REPO = os.path.join(os.path.dirname(__file__), "..")
_PREFLIGHT = os.path.join(_REPO, "shared-memory", "scripts", "preflight.sh")

_WARN_MARKER = "resolves ONLY when your shell profile is loaded"
_OK_MARKER = "also resolves on the system default PATH"


def _make_stub(dir_path, name, body):
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _stub_env(tmp_path, sys_path_answer, uv_broken=False, poison_python=False):
    """Build agentbin/uv + binstub/getconf, return env with PATH prefixed by
    them (real PATH kept after, so docker/git/awk/etc. still resolve).

    sys_path_answer: what the fake `getconf PATH` prints — what the check
    treats as the platform's default PATH.
    uv_broken: if set, the stub `uv` exits 1 and prints nothing when
    EXECUTED — simulating a present-but-non-functional binary, so the test
    can prove the reachability check's answer never depends on uv actually
    running, only on `command -v` finding it on PATH (I-P2).
    poison_python: if set, `python`/`python3` on PATH fail loudly if invoked
    — proves the check does not depend on a python interpreter (I-P2).
    """
    agentbin = tmp_path / "agentbin"
    binstub = tmp_path / "binstub"
    if uv_broken:
        uv_body = "#!/usr/bin/env bash\nexit 1\n"
    else:
        uv_body = "#!/usr/bin/env bash\necho 'uv 0.1.0 (test stub)'\n"
    _make_stub(str(agentbin), "uv", uv_body)
    getconf_body = f'#!/usr/bin/env bash\necho "{sys_path_answer}"\n'
    _make_stub(str(binstub), "getconf", getconf_body)
    if poison_python:
        fail_body = "#!/usr/bin/env bash\necho 'python must not run here' >&2\nexit 1\n"
        _make_stub(str(agentbin), "python", fail_body)
        _make_stub(str(agentbin), "python3", fail_body)

    env = dict(os.environ)
    env["PATH"] = f"{agentbin}{os.pathsep}{binstub}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_preflight(env):
    return subprocess.run(["bash", _PREFLIGHT], capture_output=True, text=True,
                          env=env, cwd=_REPO, timeout=60)


# ── I-P1: a profile-only uv is reported as a WARNING naming the agent
#    consequence — never silently reported as "present" ────────────────────

def test_profile_only_uv_is_reported_as_a_warning(tmp_path):
    env = _stub_env(tmp_path, sys_path_answer="/nonexistent-test-sysdir-i-p1")
    result = _run_preflight(env)
    out = result.stdout
    assert _WARN_MARKER in out, (
        "preflight did not warn when uv resolves only via the operator's "
        f"own shell profile:\n{out}"
    )
    assert "agent" in out.lower() and "silent" in out.lower(), (
        "the warning does not name the agent-facing, silent failure "
        "consequence — a generic 'uv path issue' is not enough"
    )
    # Additive, not a replacement: the existing operator-level check must
    # still report uv as present.
    assert "uv (0.1.0)" in out, "the existing operator-level uv check regressed"


def test_operator_only_absence_is_still_a_hard_failure(tmp_path):
    """The pre-existing check is untouched: no uv anywhere on PATH at all
    must still fail preflight outright, never merely warn."""
    env = dict(os.environ)
    # Use a minimal PATH that cannot resolve uv, but keeps other required
    # tools reachable so the rest of the script still runs to completion.
    env["PATH"] = "/usr/bin:/bin"
    # Guard: this assumption only holds if uv is not ALSO on /usr/bin:/bin
    # on the machine running the suite; skip rather than false-fail if so.
    probe = subprocess.run(["env", "-i", "PATH=/usr/bin:/bin", "sh", "-c",
                            "command -v uv"], capture_output=True, text=True)
    if probe.returncode == 0:
        import pytest
        pytest.skip("uv is on this host's minimal PATH — precondition not met")
    result = _run_preflight(env)
    assert result.returncode != 0
    assert "uv not found" in result.stdout


# ── I-P2: the check itself never depends on uv or a python interpreter ─────

def test_the_check_answers_correctly_even_when_uv_itself_is_non_functional(tmp_path):
    """`command -v uv` tests PRESENCE on PATH, never RUNS the binary. Prove it
    the only way that is conclusive: make the stub `uv` exit 1 (print
    nothing) the moment it is actually executed, and confirm the
    reachability check still answers correctly regardless — its answer
    cannot be coming from running uv, because running uv here produces
    nothing usable. (The unrelated, PRE-EXISTING `uv --version` line still
    runs uv, by design — that is the operator-facing check this file does
    not touch; this test is scoped to the NEW reachability line only.)"""
    env = _stub_env(tmp_path, sys_path_answer="/nonexistent-test-sysdir-i-p2",
                    uv_broken=True)
    result = _run_preflight(env)
    assert _WARN_MARKER in result.stdout, (
        "the reachability check gave no answer once uv stopped being "
        f"functional — it must depend only on `command -v`, not execution:\n"
        f"{result.stdout}"
    )


def test_no_python_interpreter_is_invoked_by_the_check(tmp_path):
    """Hide python/python3 behind stubs that fail loudly if invoked, and
    confirm the check still runs and answers correctly regardless — proving
    it has no python dependency, direct or incidental."""
    env = _stub_env(tmp_path, sys_path_answer="/nonexistent-test-sysdir-i-p2b",
                    poison_python=True)
    result = _run_preflight(env)
    assert _WARN_MARKER in result.stdout, (
        "the check did not complete correctly with python entirely replaced "
        f"by a failing stub:\n{result.stdout}\n{result.stderr}"
    )
    assert "python must not run here" not in result.stderr, (
        "a python stub was actually invoked by preflight.sh"
    )


# ── I-P3: a correctly-set-up host produces NO warning ───────────────────────

def test_uv_on_the_system_default_path_produces_no_warning(tmp_path):
    """When the fake getconf's answer INCLUDES the directory the stub uv
    lives in — the "operator did everything right" case — no warning, and
    the positive confirmation line is printed instead."""
    agentbin = tmp_path / "agentbin"
    env = _stub_env(tmp_path, sys_path_answer=f"{agentbin}:/usr/bin:/bin")
    result = _run_preflight(env)
    out = result.stdout
    assert _WARN_MARKER not in out, (
        f"a false alarm was raised on a correctly-set-up host:\n{out}"
    )
    assert _OK_MARKER in out, (
        f"no positive confirmation was printed for the correctly-set-up case:\n{out}"
    )


def test_getconf_unavailable_makes_no_claim_either_way(tmp_path):
    """If `getconf PATH` cannot answer at all, the check has nothing to
    measure — it must say nothing rather than assert either a warning or an
    all-clear it has no basis for. Everything else on PATH stays real (docker,
    git, awk, ...) — only getconf itself is shadowed with a stub that fails,
    the same way a host lacking PATH-variable support would."""
    agentbin = tmp_path / "agentbin"
    binstub = tmp_path / "binstub"
    _make_stub(str(agentbin), "uv",
              "#!/usr/bin/env bash\necho 'uv 0.1.0 (test stub)'\n")
    _make_stub(str(binstub), "getconf", "#!/usr/bin/env bash\nexit 1\n")
    env = dict(os.environ)
    env["PATH"] = f"{agentbin}{os.pathsep}{binstub}{os.pathsep}{env.get('PATH', '')}"
    result = _run_preflight(env)
    out = result.stdout
    assert _WARN_MARKER not in out
    assert _OK_MARKER not in out
