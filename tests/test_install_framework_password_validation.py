"""install_framework.sh — ask_secret() (framework fact:1499 CRITICAL 1, and
its W7 round-3 sequel on the AGENT install path).

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

W7 round 3 (fact:1499 CLASS, this time on AGENTS.md's own Phase 1): the fix
above validated length, but AGENTS.md still had THE AGENT generate both
passwords itself (`openssl rand -hex 20` in the agent's own shell) and pipe
them in -- the value existed in the agent's shell and its transcript, the
exact class fact:1499 names. The fix: an EMPTY answer to ask_secret() now
means "generate a strong password INTERNALLY, in this process, via
python3's secrets module" -- no agent, human or script ever holds the
plaintext outside this one function.

The fix, and what these tests pin:
  - a NON-EMPTY password of length <= 8 is refused; length > 8 is accepted
  - on an invalid (non-empty, too-short) entry with more input still
    available on stdin, ask_secret() RE-PROMPTS rather than falling through
  - an EMPTY answer generates a strong (40 hex char) password internally,
    returns it via the SAME stdout-capture contract as a typed password, and
    does not re-prompt or refuse it -- it is now a TERMINAL, valid answer
  - two separate empty-answer calls generate two DIFFERENT passwords (this
    is real randomness, not a fixed/cached value)
  - the generated value is never interpolated into any command's argv --
    the python3 invocation is a fixed, literal script with no shell
    variable inside it that could carry a secret onto a process's argv
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


def test_short_then_valid_reprompts_until_valid():
    """A NON-EMPTY too-short entry must still RE-PROMPT (loop back), never
    fall through and accept it or abort early -- the length-validation loop
    is unchanged by the W7 round-3 empty-answer behaviour."""
    proc = _run("short\nvalidpassword123\n")
    assert proc.returncode == 0
    assert proc.stdout == "validpassword123"
    assert proc.stderr.count("must be more than 8 characters") == 1


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


def test_empty_answer_generates_a_strong_password_internally():
    """W7 round 3: an EMPTY answer is now a TERMINAL, valid answer -- it
    generates a password right there and returns it via the same
    stdout-capture contract as a typed one, rather than re-prompting."""
    proc = _run("\n")
    assert proc.returncode == 0
    # 40 hex characters (secrets.token_hex(20)) -- also proves it never
    # contains '/', which install_framework.sh's own Neo4j-password loop
    # separately refuses.
    assert re.fullmatch(r"[0-9a-f]{40}", proc.stdout), (
        f"generated password does not look like 40 lowercase hex chars: {proc.stdout!r}"
    )
    # Never re-prompted, never refused it as "too short" (it is not empty by
    # the time the length check would run -- it is intercepted before that).
    assert "must be more than 8 characters" not in proc.stderr
    # THE VALUE ITSELF must never appear anywhere on stderr (the only other
    # stream this function writes to) -- stdout-capture is the sole path.
    assert proc.stdout not in proc.stderr


def test_two_empty_answers_generate_different_passwords():
    """Proves real randomness, not a fixed or cached value -- two SEPARATE
    process invocations, each given an empty answer, must not collide."""
    first = _run("\n")
    second = _run("\n")
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout != second.stdout
    assert re.fullmatch(r"[0-9a-f]{40}", first.stdout)
    assert re.fullmatch(r"[0-9a-f]{40}", second.stdout)


def test_generated_password_never_reaches_a_process_argv():
    """The python3 invocation ask_secret() uses to generate a password must
    be a FIXED, LITERAL script -- never a shell variable holding the secret
    interpolated into the command line, which would put the value on
    /proc/<pid>/cmdline (world-readable while the process lives)."""
    source = _extract_ask_secret_source()
    m = re.search(r"python3\s+-c\s+'([^']*)'", source)
    assert m, "ask_secret() no longer generates via a literal `python3 -c '...'` call"
    py_script = m.group(1)
    assert "secrets" in py_script and "token_hex" in py_script, (
        "the python3 generator no longer uses secrets.token_hex"
    )
    # The literal script must not reference $v, $1, or any other shell
    # expansion -- it must be pure Python, argv-free of any secret.
    assert "$" not in py_script, (
        f"the python3 generator script contains a shell expansion ({py_script!r}) "
        "-- this could put a secret value on python3's own argv"
    )


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
    """A pipe that supplies some (non-empty, invalid) answers and then runs
    dry must still fail loudly -- exhaustion, not just an immediately-closed
    stdin, is the trigger. (An EMPTY line is no longer an invalid attempt as
    of W7 round 3 -- see test_empty_answer_generates_a_strong_password_internally
    -- so this uses only non-empty too-short answers to stay a genuine test
    of the exhaustion path.)"""
    proc = _run("short\nshortish\n")  # both non-empty, both invalid, then EOF
    assert proc.returncode != 0
    assert LABEL in proc.stderr
    assert proc.stdout == ""
