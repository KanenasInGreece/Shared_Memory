"""update_skill.sh must not blame the GATEWAY for a client-side failure.

MEASURED DEFECT (found on a clean install, 2026-08-23). The compatibility step
ran `python3 "$SCRIPT_DIR/memory_bridge.py" doctor` and treated ANY non-zero
exit as version skew, printing:

    ⚠ Updated to X but still incompatible. The GATEWAY itself needs upgrading

Two things were wrong, and they compound:

  * The invocation was not the documented one. README publishes
    `uv run --with httpx python .../memory_bridge.py doctor`, and the
    invocation line IS the contract. A bare `python3` only works where httpx
    happens to be importable globally — true on a development box, false on a
    clean install. So the check passed wherever it was written and failed
    wherever it mattered.
  * `doctor` exits 1 both for a real version verdict and for an interpreter
    crash, so the exit code cannot tell them apart. On the host where this was
    found, the message accused a gateway that was healthy, current, and passed
    full postflight minutes later.

This is the third instance in one release cycle of a check blaming a component
it never reached, so the distinguishing rule is pinned by test: a gateway
verdict may only be reported when the client actually PRINTED one.

Drives the REAL shipped script via SHARED_MEMORY_UPDATE_RAW_BASE=file://…,
the same override sync_skills.sh uses.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
UPDATE_SRC = REPO_ROOT / "shared-memory" / "scripts" / "update_skill.sh"

# A `uv` that behaves like the real one for `uv run --with X python ...`:
# strips its own flags and execs the interpreter. Its presence is what proves
# the script reaches for the DOCUMENTED invocation rather than a bare python3.
FAKE_UV = """#!/usr/bin/env bash
echo "FAKE-UV-INVOKED $*" >> "$CAPTURE_DIR/uv_calls.txt"
[[ "$1" == "run" ]] || exit 0
shift
while [[ "$1" == --with ]]; do shift 2; done
[[ "$1" == python* ]] && shift
exec python3 "$@"
"""

BRIDGE_CRASHES = """import sys
raise ModuleNotFoundError("No module named 'httpx'")
"""

# A crash whose traceback MENTIONS compat. Review finding: matching the bare word
# would read this as a gateway verdict and bring the false accusation back.
BRIDGE_CRASHES_MENTIONING_COMPAT = """import sys
diag = {}
print(diag["compat"])
"""

BRIDGE_REPORTS_SKEW = """import sys
print('{\\n  "compat": "client too old",\\n  "gateway_api_version": 5\\n}')
sys.exit(1)
"""

BRIDGE_OK = """import sys
print('{\\n  "compat": "ok"\\n}')
sys.exit(0)
"""


def _farm_without(tmp_path, *hidden):
    """A single directory of symlinks to everything on PATH except `hidden`."""
    farm = tmp_path / "farm"
    farm.mkdir(exist_ok=True)
    seen = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name in hidden or name in seen:
                continue
            seen.add(name)
            try:
                os.symlink(os.path.join(d, name), farm / name)
            except OSError:
                pass
    return farm


def _run(tmp_path, bridge_src, with_uv=True):
    """Build a fake skill source tree + install dir, run the real script."""
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "memory_bridge.py").write_text(
        'VERSION = "9.9.9"\n' + bridge_src)
    (src / "scripts" / "update_skill.sh").write_text(UPDATE_SRC.read_text())
    (src / "SKILL.md").write_text("# fake skill\n")
    (src / "MANIFEST.txt").write_text("SKILL.md\nscripts/memory_bridge.py\n")

    install = tmp_path / "install"
    (install / "scripts").mkdir(parents=True)
    shutil.copy(UPDATE_SRC, install / "scripts" / "update_skill.sh")
    (install / "scripts" / "memory_bridge.py").write_text('VERSION = "0.0.1"\n')

    capture = tmp_path / "capture"
    capture.mkdir()
    binpath = tmp_path / "bin"
    binpath.mkdir()
    if with_uv:
        uv = binpath / "uv"
        uv.write_text(FAKE_UV)
        uv.chmod(uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    env["SHARED_MEMORY_UPDATE_RAW_BASE"] = f"file://{src}"
    env["CAPTURE_DIR"] = str(capture)
    # A PATH that carries our fake uv (or deliberately lacks one) but keeps the
    # real coreutils/curl the script needs.
    if with_uv:
        env["PATH"] = f"{binpath}:{env.get('PATH','')}"
    else:
        # A PATH farm mirroring the real one MINUS uv, so `command -v uv`
        # genuinely fails while bash, curl and python3 still resolve. Dropping
        # whole directories instead would take /usr/bin — and bash with it.
        env["PATH"] = str(_farm_without(tmp_path, "uv"))

    res = subprocess.run(
        ["bash", str(install / "scripts" / "update_skill.sh")],
        capture_output=True, text=True, timeout=120, env=env,
    )
    calls = capture / "uv_calls.txt"
    return res, (calls.read_text() if calls.exists() else "")


def test_a_client_that_cannot_run_is_not_reported_as_a_stale_gateway(tmp_path):
    """THE regression. The client never reached the gateway, so no claim about
    the gateway is admissible."""
    res, _ = _run(tmp_path, BRIDGE_CRASHES)
    out = res.stdout + res.stderr

    assert "the compatibility check could not RUN" in out
    assert "The GATEWAY itself" not in out, (
        "a client-side crash was reported as a stale gateway — the accusation "
        "this test exists to prevent")


def test_a_client_that_cannot_run_names_the_real_cause(tmp_path):
    """An operator acts on this message, so it has to hand over a runnable next
    step. Asserted as the REQUIREMENT — the message names the dependency the
    documented invocation supplies — rather than pinning a hardcoded word, which
    would pass just as happily if the advice became wrong."""
    res, _ = _run(tmp_path, BRIDGE_CRASHES)
    out = res.stdout + res.stderr

    assert "could not RUN" in out
    # Whatever dependency the invocation supplies must be the one named.
    import re
    m = re.search(r"uv run --with (\S+)", out)
    assert m, "no runnable next step offered"
    assert m.group(1) in out.split("could not RUN", 1)[1]


def test_a_crash_that_merely_mentions_compat_is_not_a_gateway_verdict(tmp_path):
    """Review finding (all three reviewers): the branch decided on a substring.
    A traceback containing the word compat would be read as a verdict, restoring
    the exact false accusation this change exists to remove. Matching the JSON
    KEY is what separates 'the client printed an object' from 'the client
    happened to say the word'."""
    res, _ = _run(tmp_path, BRIDGE_CRASHES_MENTIONING_COMPAT)
    out = res.stdout + res.stderr

    assert "The GATEWAY itself" not in out, (
        "a client crash mentioning 'compat' was reported as a stale gateway")
    assert "could not RUN" in out


def test_a_real_version_verdict_still_blames_the_gateway(tmp_path):
    """The fix must not swing the other way: when the client RAN and returned a
    verdict, the gateway message is the correct one and must survive."""
    res, _ = _run(tmp_path, BRIDGE_REPORTS_SKEW)
    out = res.stdout + res.stderr

    assert "The GATEWAY itself" in out
    assert "could not RUN" not in out


def test_the_documented_invocation_is_the_one_used(tmp_path):
    """The published contract is `uv run --with httpx python … doctor`. The bare
    python3 form only worked where httpx was globally importable."""
    _, uv_calls = _run(tmp_path, BRIDGE_OK)

    assert "FAKE-UV-INVOKED" in uv_calls, "uv was never invoked"
    assert "--with httpx" in uv_calls
    assert "doctor" in uv_calls


def test_a_healthy_client_reports_compat_ok(tmp_path):
    res, _ = _run(tmp_path, BRIDGE_OK)
    out = res.stdout + res.stderr

    assert "compat: ok" in out
    assert res.returncode == 0


def test_without_uv_it_still_attempts_rather_than_refusing(tmp_path):
    """A host with httpx installed system-wide must still get a real verdict —
    the fix is about attribution, not about mandating uv."""
    res, uv_calls = _run(tmp_path, BRIDGE_OK, with_uv=False)
    out = res.stdout + res.stderr

    assert uv_calls == ""
    assert "compat: ok" in out
