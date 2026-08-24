"""install_framework.sh — ask_secret() (framework fact:1499 CRITICAL 1).

Only the deterministic ask_secret() function is testable here. It is
embedded in shared-memory/scripts/install_framework.sh (between the
`# >>> ASK_SECRET` / `# <<< ASK_SECRET` markers) rather than
duplicated: this file extracts that block VERBATIM and runs it standalone
via subprocess, feeding controlled stdin, so the test exercises the actual
shipped source -- never a hand-written reimplementation that could silently
drift from it.

fact:1499 CRITICAL 1 (measured on a live Ubuntu install): the installer
suggested `openssl rand -hex 20` for the Neo4j/Postgres passwords but
validated nothing -- pressing Enter (or piping stdin with nothing left to
answer with) wrote NEO4J_PASSWORD= / PG_PASSWORD= as literal empty strings,
and the install reported success anyway.

The fix, and what these tests pin:
  - a password of length <= 8 (including empty) is refused
  - a password of length > 8 is accepted
  - on an invalid entry with more input still available on stdin (a real
    terminal, or a script/pipe feeding a sequence of scripted answers),
    ask_secret() RE-PROMPTS rather than falling through
  - on EXHAUSTED stdin (closed, or a pipe with no more lines -- `read`
    itself fails), ask_secret() FAILS LOUDLY: nonzero exit, a message
    naming the password step, and it never echoes a password to stdout
"""
import re
import subprocess
from pathlib import Path

INSTALL_FRAMEWORK = (
    Path(__file__).parent.parent / "shared-memory" / "scripts" / "install_framework.sh"
)

BEGIN_MARKER = "# >>> ASK_SECRET"
END_MARKER = "# <<< ASK_SECRET"

LABEL = "Test password"


def _extract_ask_secret_source() -> str:
    text = INSTALL_FRAMEWORK.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{INSTALL_FRAMEWORK} -- the extraction markers moved or were removed"
    )
    return m.group(1)


def _run(stdin_text) -> subprocess.CompletedProcess:
    """Run ask_secret(LABEL) standalone. stdin_text=None closes stdin
    entirely (simulates a fully non-interactive invocation, e.g. `</dev/null`
    or a CI runner with no controlling terminal); a str feeds that exact text
    as scripted answers, one `read` per line."""
    source = _extract_ask_secret_source()
    script = source + f"\nask_secret '{LABEL}'"
    kwargs = {}
    if stdin_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=15, **kwargs,
    )


def test_markers_present_exactly_once():
    text = INSTALL_FRAMEWORK.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_the_function():
    source = _extract_ask_secret_source()
    assert "ask_secret()" in source


def test_valid_password_accepted_and_echoed(tmp_path=None):
    """A password of length > 8 is accepted on the first try; the exact
    value the caller typed is what gets echoed back (and would be written
    to .env by the caller)."""
    proc = _run("supersecret123\n")
    assert proc.returncode == 0
    assert proc.stdout == "supersecret123"


def test_empty_then_short_then_valid_reprompts_until_valid():
    """Interactive-shaped path: empty (Enter), then too-short, then a valid
    password -- each invalid entry must RE-PROMPT (loop back), never fall
    through and accept the bad value or abort early."""
    proc = _run("\nshort\nvalidpassword123\n")
    assert proc.returncode == 0
    assert proc.stdout == "validpassword123"
    # Both invalid attempts must have been named on stderr -- proves the
    # loop actually re-prompted twice, not that it silently retried.
    assert proc.stderr.count("must be more than 8 characters") == 2


def test_length_exactly_8_refused_length_9_accepted():
    """Boundary pinned by VALUE, not by comparing two expressions: 8 chars
    is refused (one retry needed), 9 chars is accepted outright."""
    refused = _run("12345678\n")  # 8 chars, then stdin exhausted -> hard fail
    assert refused.returncode != 0
    assert "must be more than 8 characters" in refused.stderr

    accepted_after_boundary = _run("12345678\n123456789\n")  # 8 then 9
    assert accepted_after_boundary.returncode == 0
    assert accepted_after_boundary.stdout == "123456789"

    accepted_outright = _run("123456789\n")  # 9 chars, first try
    assert accepted_outright.returncode == 0
    assert accepted_outright.stdout == "123456789"


def test_closed_stdin_fails_loudly_naming_the_step():
    """Non-interactive with no stdin at all (measured failure mode: piping
    stdin ran the whole install silently on defaults) -- must be a hard,
    nonzero-exit failure that names the password step, never a silent empty
    string on stdout."""
    proc = _run(None)
    assert proc.returncode != 0
    assert LABEL in proc.stderr
    assert proc.stdout == ""


def test_exhausted_pipe_fails_loudly_after_invalid_attempts():
    """A pipe that supplies some (invalid) answers and then runs dry must
    still fail loudly -- exhaustion, not just an immediately-closed stdin,
    is the trigger."""
    proc = _run("\nshort\n")  # both invalid, then EOF -- never a valid one
    assert proc.returncode != 0
    assert LABEL in proc.stderr
    assert proc.stdout == ""
