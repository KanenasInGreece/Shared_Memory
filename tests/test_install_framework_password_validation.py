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

W7/F7 (this round) closes the footgun that fix created. install_framework.sh
still offers `Overwrite? [y/N]` on an existing .env -- and on that path an
empty answer was a *successful* rotation to a value the operator is told is
"not displayed, not logged", while the already-initialised Postgres and Neo4j
volumes still required the OLD password. Both stores would then refuse auth,
with no way back. Generation is therefore FIRST-INSTALL-ONLY, selected by an
explicit second argument -- never by reading the live .env from inside this
function, which would break this standalone harness under `set -u` and turn
the suite red on any checkout that merely has an install already.

W7/F9 hardens the generator itself: `python3 -I` (isolated -- no
sitecustomize, no user site-packages, no PYTHONPATH), and the captured value
is validated as exactly 40 hex characters AFTER `$(...)` has stripped the
trailing newline. Measured: a `python3` earlier on PATH that prints a warning
line before the hex exits 0 and hands back "WARNING_ON_STDOUT\\n<hex>", which
was accepted as the password and broke the container's
NEO4J_AUTH=neo4j/<password> parsing.

The fix, and what these tests pin:
  - a NON-EMPTY password of length <= 8 is refused; length > 8 is accepted
  - on an invalid (non-empty, too-short) entry with more input still
    available on stdin, ask_secret() RE-PROMPTS rather than falling through
  - in FIRST-INSTALL mode an EMPTY answer generates a strong (40 hex char)
    password internally, returns it via the SAME stdout-capture contract as a
    typed password, and does not re-prompt -- a TERMINAL, valid answer
  - in OVERWRITE mode an EMPTY answer generates NOTHING: it re-prompts, and
    aborts on exhausted stdin rather than writing a password nobody knows
  - an unknown mode is refused rather than guessed, and both shipped call
    sites pass the mode explicitly
  - two separate empty-answer calls generate two DIFFERENT passwords (this
    is real randomness, not a fixed/cached value)
  - the generated value is never interpolated into any command's argv --
    the python3 invocation is a fixed, literal `python3 -I -c` script with no
    shell variable inside it that could carry a secret onto a process's argv
  - a python3 whose stdout is not exactly 40 hex characters is REFUSED, and
    a correct generator still passes that validation
  - the prompt no longer advertises a shell command for the operator to run
  - on EXHAUSTED stdin (closed, or a pipe with no more lines -- `read`
    itself fails), ask_secret() FAILS LOUDLY: nonzero exit, a message
    naming the password step, and it never echoes a password to stdout
"""
import os
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


def _run(stdin_text, mode=None, env=None) -> subprocess.CompletedProcess:
    """Run ask_secret(LABEL) standalone. stdin_text=None closes stdin
    entirely (simulates a fully non-interactive invocation, e.g. `</dev/null`
    or a CI runner with no controlling terminal); a str feeds that exact text
    as scripted answers, one `read` per line.

    `mode` is ask_secret's second argument (W7/F7: "first-install" lets an
    empty answer generate, "overwrite" makes it re-prompt). Left off entirely
    when None, which is how the standalone default -- `${2:-first-install}` --
    stays exercised rather than assumed.
    """
    source = _extract_ask_secret_source()
    call = f"ask_secret '{LABEL}'" + (f" '{mode}'" if mode is not None else "")
    script = source + "\n" + call
    kwargs = {}
    if stdin_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text
    if env is not None:
        kwargs["env"] = env
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
    m = re.search(r"python3\s+-I\s+-c\s+'([^']*)'", source)
    assert m, (
        "ask_secret() no longer generates via a literal `python3 -I -c '...'` "
        "call -- -I (isolated) is part of the contract: it drops "
        "sitecustomize, the user site directory and PYTHONPATH, so nothing a "
        "third party can drop on this box gets to run inside the generator"
    )
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


# --- W7/F7: generation is FIRST-INSTALL-ONLY -------------------------------


def test_first_install_mode_generates_on_an_empty_answer():
    """The path where generating is correct: no shared-memory/.env exists yet,
    so there is no already-initialised database expecting an older value."""
    proc = _run("\n", mode="first-install")
    assert proc.returncode == 0
    assert re.fullmatch(r"[0-9a-f]{40}", proc.stdout), (
        f"first-install mode did not generate 40 hex chars: {proc.stdout!r}"
    )


def test_overwrite_mode_reprompts_instead_of_generating():
    """⭐ THE FOOTGUN THIS CLOSES. install_framework.sh offers `Overwrite?
    [y/N]` on an existing .env. If an empty answer generated there, it would
    be a *successful* rotation to a value the operator is told is "not
    displayed, not logged" -- while the already-initialised Postgres and Neo4j
    volumes still require the OLD password. Both stores would then refuse
    auth, with no way back.

    So: empty re-prompts, and the next (valid) answer is what is returned. No
    generated value may appear on stdout."""
    proc = _run("\nthe-real-password\n", mode="overwrite")
    assert proc.returncode == 0
    assert proc.stdout == "the-real-password"
    assert not re.search(r"[0-9a-f]{40}", proc.stdout), (
        "overwrite mode generated a password on an empty answer"
    )
    assert "cannot generate" in proc.stderr


def test_overwrite_mode_aborts_rather_than_writing_a_password_nobody_knows():
    """An empty answer followed by exhausted stdin must FAIL, not fall back to
    generating. Writing a password nobody knows into a .env whose databases
    expect a different one is the outcome this whole item exists to prevent."""
    proc = _run("\n", mode="overwrite")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert LABEL in proc.stderr


def test_an_unknown_mode_is_refused_rather_than_guessed():
    """Whether an empty answer may generate is a security decision. A typo in
    the caller must abort, never fall back to the permissive branch."""
    proc = _run("\n", mode="probably-first-install")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "unknown mode" in proc.stderr


def test_both_shipped_call_sites_pass_the_mode_explicitly():
    """The default exists for the standalone harness only. The installer
    itself must never rely on it -- and the mode must come from the state of
    the .env captured BEFORE anything is written, not re-derived later."""
    text = INSTALL_FRAMEWORK.read_text()
    # Only real call sites: `VAR="$(ask_secret ...)"`. Prose in the comments
    # above the function mentions the name too, and must not be mistaken for
    # an invocation.
    invocations = [
        line.strip()
        for line in text.split("\n")
        if "$(ask_secret " in line
    ]
    assert len(invocations) >= 2, (
        f"expected at least the two password call sites, found {invocations!r}"
    )
    for call in invocations:
        assert "$SECRET_MODE" in call, (
            f"ask_secret call site does not pass the mode explicitly: {call!r}"
        )
    assert re.search(r'^SECRET_MODE="first-install"', text, re.M), (
        "SECRET_MODE is not initialised to first-install before the .env check"
    )
    assert re.search(r'^\s*SECRET_MODE="overwrite"', text, re.M), (
        "SECRET_MODE is never set to overwrite when an existing .env is found"
    )


def test_the_prompt_no_longer_tells_the_operator_to_run_a_generator():
    """W7/F10: a password the operator generates in their own shell lives in
    their history, and on the agent install path in a transcript (fact:1499).
    The prompt must not advertise one."""
    text = INSTALL_FRAMEWORK.read_text()
    prompts = re.findall(r"ask_secret\s+\"([^\"]*)\"", text)
    assert prompts, "no ask_secret prompt strings found"
    for prompt in prompts:
        assert "openssl" not in prompt, (
            f"the password prompt still advertises a shell generator: {prompt!r}"
        )


# --- W7/F9: a generator that returns rc 0 and garbage must be refused ------


def _stub_python3_dir(tmp_path, body):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text(body)
    stub.chmod(0o755)
    return stub_dir


def test_a_python3_that_prints_a_warning_before_the_hex_is_refused(tmp_path):
    """⭐ MEASURED, and the reason the exit status alone is not enough: a
    `python3` earlier on PATH that prints a warning line before the hex exits
    0 and hands back "WARNING_ON_STDOUT\\n<hex>". That was ACCEPTED as the
    password, and then broke the container's NEO4J_AUTH=neo4j/<password>
    parsing, which splits on '/' and cannot survive a newline either."""
    stub_dir = _stub_python3_dir(
        tmp_path,
        "#!/bin/sh\n"
        "echo WARNING_ON_STDOUT\n"
        "echo 0123456789abcdef0123456789abcdef01234567\n",
    )
    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ.get('PATH', '')}")
    proc = _run("\n", mode="first-install", env=env)
    assert proc.returncode != 0, (
        f"a python3 printing a warning before the hex was ACCEPTED: "
        f"stdout={proc.stdout!r}"
    )
    assert proc.stdout == "", (
        f"a value escaped to stdout despite the refusal: {proc.stdout!r}"
    )
    assert "40-character hex" in proc.stderr


def test_a_python3_returning_a_short_value_is_refused(tmp_path):
    """Same guard from the other side: rc 0, no extra output, but not the
    40-hex shape the rest of the install depends on."""
    stub_dir = _stub_python3_dir(tmp_path, "#!/bin/sh\necho deadbeef\n")
    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ.get('PATH', '')}")
    proc = _run("\n", mode="first-install", env=env)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "40-character hex" in proc.stderr


def test_a_correct_generator_is_not_broken_by_the_validation(tmp_path):
    """The validation runs AFTER `$(...)` strips the trailing newline.
    Validating before that strip would make a CORRECT generator fail its own
    check -- so a stub that behaves exactly like the real one must pass."""
    stub_dir = _stub_python3_dir(
        tmp_path,
        "#!/bin/sh\necho 0123456789abcdef0123456789abcdef01234567\n",
    )
    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ.get('PATH', '')}")
    proc = _run("\n", mode="first-install", env=env)
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    assert proc.stdout == "0123456789abcdef0123456789abcdef01234567"
