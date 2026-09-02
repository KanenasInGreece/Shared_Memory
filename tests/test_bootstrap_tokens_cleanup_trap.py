"""bootstrap_tokens.sh — I.2 (SEC round, ADV1-8): the script-scope temp-file
cleanup trap.

⛔ This file NEVER executes bootstrap_tokens.sh (nor generate_tokens.py) —
not even a function import (`fact:1471`, a standing rule for every builder
in this repo: those two scripts write to the REAL $HOME regardless of any
sandbox argument passed to them). Every test here EXTRACTS, via a plain
text slice keyed on line anchors, only the tiny script-scope fragment this
item actually changed — the `_CLEANUP_PATHS` array, `_cleanup_temp_files()`,
the `trap ... EXIT INT TERM` line, and `replace_registry_lines()` itself —
and sources THAT fragment (with `mv` shadowed by a test stub) inside a
throwaway bash process that never parses bootstrap_tokens.sh's own
argument list and never calls generate_tokens.py. Extracting by ANCHOR
rather than hand-copying the fragment's text means a future edit to the
real function is what this test actually exercises, not a frozen
duplicate that could silently drift from it.

ADV1-8's defect (fixed by this item): a NAIVE `trap 'rm -f "$tmp"' EXIT`
set INSIDE replace_registry_lines() is a no-op — bash traps are
process-global, but `local tmp` goes out of scope the moment the function
RETURNS, so by the time the trap fires (at process exit) the variable has
long since expanded to an empty string. The fix moves both the array and
the trap registration to script scope, appending every mktemp path as it
is created.
"""
import os
import re
import signal
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOTSTRAP_SRC = REPO_ROOT / "shared-memory" / "scripts" / "bootstrap_tokens.sh"


def _extract_cleanup_fragment() -> str:
    """The exact script-scope fragment this item touches: from
    `_CLEANUP_PATHS=()` through the closing brace of
    `replace_registry_lines()`. Anchored on the FUNCTION NAMES, never a
    line-number slice, so a harmless reflow elsewhere in the file cannot
    silently point this at the wrong text."""
    src = BOOTSTRAP_SRC.read_text()
    start_marker = "_CLEANUP_PATHS=()"
    assert start_marker in src, (
        "_CLEANUP_PATHS=() not found in bootstrap_tokens.sh -- the I.2 fix "
        "was reverted or renamed; update this test's anchor"
    )
    start = src.index(start_marker)
    func_marker = "\nreplace_registry_lines() {"
    func_start = src.index(func_marker, start)
    # The function's closing brace is the first line consisting of exactly "}"
    # after the function opens.
    body_start = func_start + len(func_marker)
    close = re.search(r"\n\}\n", src[body_start:])
    assert close, "replace_registry_lines() has no closing brace -- update this test"
    end = body_start + close.end()
    fragment = src[start:end]
    assert "_cleanup_temp_files" in fragment
    # INT/TERM each get their own explicit `exit` (bash's trap semantics:
    # naming a signal alongside EXIT with no explicit exit in ITS OWN
    # handler resumes the script after the handler returns, rather than
    # stopping it) -- three separate `trap` registrations, not one line
    # naming all three signals.
    assert re.search(r"^trap _cleanup_temp_files EXIT$", fragment, re.M), fragment
    assert re.search(r"^trap '_cleanup_temp_files; exit 130' INT$", fragment, re.M), fragment
    assert re.search(r"^trap '_cleanup_temp_files; exit 143' TERM$", fragment, re.M), fragment
    assert "mktemp" in fragment
    return fragment


def _write_harness(tmp_path: Path, env_file: Path, *, interrupt: bool) -> Path:
    """A throwaway bash script: sets ENV_FILE, shadows `mv` with a stub
    (never the real coreutils mv), sources the extracted fragment, then
    calls replace_registry_lines exactly as bootstrap_tokens.sh's own bulk-
    mint path does. When `interrupt` is True the `mv` stub sends itself
    SIGTERM and sleeps, modelling an operator's Ctrl+C landing between the
    first mktemp and the function's final rename."""
    fragment = _extract_cleanup_fragment()
    if interrupt:
        mv_stub = (
            'mv() {\n'
            '    kill -TERM $$\n'
            '    sleep 5\n'  # never reached once the TERM trap runs and exits
            '}\n'
        )
    else:
        mv_stub = 'mv() { command mv "$@"; }\n'
    script = (
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        f'ENV_FILE="{env_file}"\n'
        + mv_stub
        + fragment
        + '\nreplace_registry_lines "AGENT_TOKENS" "AGENT_TOKENS=claude:sha256:deadbeef"\n'
    )
    path = tmp_path / "harness.sh"
    path.write_text(script)
    os.chmod(path, 0o755)
    return path


def test_uninterrupted_run_leaves_no_stray_temp_files(tmp_path):
    """Sanity / negative case: a clean run (no interrupt) leaves ENV_FILE
    updated and no `ENV_FILE.XXXXXX`-shaped temp file behind — the trap
    firing at normal EXIT costs nothing when everything already moved."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=1\n")
    harness = _write_harness(tmp_path, env_file, interrupt=False)

    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr

    assert "AGENT_TOKENS=claude:sha256:deadbeef" in env_file.read_text()
    strays = [p for p in tmp_path.iterdir() if p.name.startswith(".env.") and p != env_file]
    assert strays == [], f"stray temp files after a clean run: {strays}"


def test_interrupted_run_still_removes_the_temp_file(tmp_path):
    """THE fix (ADV1-8): interrupt the process (SIGTERM, delivered from
    inside the `mv` stub, so it lands deterministically AFTER the first
    mktemp has already created a real file on disk and BEFORE any mv ever
    completes) and confirm the trap's cleanup removed it — not merely that
    the word "trap" appears in the source.

    Prove-failing-first (recorded in HANDOFF): re-run this exact test
    against the PRE-FIX shape (`local tmp; tmp=$(mktemp ...)` with a
    function-local `trap 'rm -f "$tmp"' EXIT` INSIDE
    replace_registry_lines(), no script-scope array) and the stray file
    survives -- the trap fires at process exit with `tmp` already out of
    scope, expanding to an empty string, so `rm -f ""` removes nothing.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=1\n")
    harness = _write_harness(tmp_path, env_file, interrupt=True)

    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=10)
    # The TERM trap's own handler exits 143 (128+15) -- if it instead fell
    # through to the never-reached `sleep 5` (the exact bash trap-semantics
    # footgun this fix also had to avoid: naming INT/TERM alongside EXIT
    # with no explicit `exit` in their handler resumes the script instead
    # of stopping it), this subprocess call would hit its own 10s timeout
    # instead of returning promptly.
    assert proc.returncode == 143, (
        f"expected exit 143 (TERM), got {proc.returncode} -- the SIGTERM/trap "
        f"sequence did not behave as modelled; investigate before trusting "
        f"the cleanup assertion below. stderr={proc.stderr!r}"
    )

    strays = [p for p in tmp_path.iterdir() if p.name.startswith(".env.") and p != env_file]
    assert strays == [], (
        f"temp file(s) survived an interrupt: {strays} -- the cleanup trap "
        f"did not fire, or fired with an already-out-of-scope variable"
    )
    # The original file is untouched -- the interrupt landed before the
    # final rename, so ENV_FILE must still hold exactly what it started with.
    assert env_file.read_text() == "EXISTING=1\n"


def test_cleanup_fragment_uses_a_script_scope_array_not_a_function_local_trap():
    """Direct source-shape pin, independent of the process-level test above:
    the trap is registered ONCE, outside any function, over the
    script-scope _CLEANUP_PATHS array -- never a `local` var captured by a
    trap set inside replace_registry_lines() itself (the exact ADV1-8
    no-op shape)."""
    fragment = _extract_cleanup_fragment()
    # The trap registration is not indented (top-level), not inside the
    # function body.
    lines = fragment.splitlines()
    trap_lines = [l for l in lines if l.strip().startswith("trap ")]
    assert trap_lines, "no trap registration found in the extracted fragment"
    for line in trap_lines:
        assert not line.startswith((" ", "\t")), (
            f"trap registered inside a function body (indented): {line!r} -- "
            f"must be script-scope, not function-local"
        )
    assert "_CLEANUP_PATHS=()" in fragment
    assert "_CLEANUP_PATHS+=(" in fragment
