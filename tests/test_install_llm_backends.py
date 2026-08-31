"""install_llm_backends.sh -- M-5/S-05-aware access elicitation (W0 item ①).

Before this, the script wrote {url, weight, model?, token_env?} only -- never
`private_ok` or `roles` -- so a credentialed backend it just configured made
the gateway refuse to start (M-5: a credentialed backend needs an EXPLICIT
private_ok or roles choice). Auth-off installs additionally hit S-05
(AGENT_TOKENS unset + any credentialed backend -> SystemExit) with no
warning from this script at all.

The fix: `build_backend_entry()` (between the `# >>> BACKEND_ACCESS` /
`# <<< BACKEND_ACCESS` markers in the real script) now elicits the M-5
choice for a credentialed backend, elicits a general-traffic/roles choice
for an uncredentialed one, warns about S-05 when AGENT_TOKENS is unset, and
never writes the one shape that newly bricks an install (private_ok: false)
or the one shape that is fatal at the gateway (roles: []).

This test extracts that block VERBATIM (same technique as
tests/test_install_framework_password_validation.py's ASK_SECRET
extraction) and runs `build_backend_entry` standalone via subprocess with
scripted stdin -- exercising the actual shipped source, never a
reimplementation that could silently drift from it. Per the brief: the
JSON-value cases must see the finished entry on stdout with every
prompt/warning/caveat on stderr (the script's own `entry="$(build_backend_
entry ...)"` capture idiom depends on that separation); the auth-warning
cases drive a temp ENV_FILE rather than the real shared-memory/.env.
"""
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_LLM_BACKENDS = (
    Path(__file__).parent.parent / "shared-memory" / "ops" / "install_llm_backends.sh"
)

BEGIN_MARKER = "# >>> BACKEND_ACCESS"
END_MARKER = "# <<< BACKEND_ACCESS"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed (this script's own hard prerequisite)"
)


def _extract_block() -> str:
    text = INSTALL_LLM_BACKENDS.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{INSTALL_LLM_BACKENDS} -- the extraction markers moved or were removed"
    )
    return m.group(1)


def _run(
    stdin_text: str,
    env_file: Path,
    url: str = "http://backend.example:9/v1",
    weight: str = "1",
    model: str = "",
    token_env: str = "",
    timeout: float = 15,
) -> subprocess.CompletedProcess:
    source = _extract_block()
    invocation = "build_backend_entry {} {} {} {} {}".format(
        shlex.quote(url), shlex.quote(weight), shlex.quote(model),
        shlex.quote(token_env), shlex.quote(str(env_file)),
    )
    # M7 (fix round): run under the SAME `set -euo pipefail` the real
    # script has at its own top -- a bug that only misbehaves under set -e
    # (an unguarded command whose failure would abort the real script)
    # could otherwise pass here while still being live in production.
    script = "set -euo pipefail\n" + source + "\n" + invocation
    return subprocess.run(
        ["bash", "-c", script],
        input=stdin_text, capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Extraction sanity
# ---------------------------------------------------------------------------

def test_markers_present_exactly_once():
    text = INSTALL_LLM_BACKENDS.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_build_backend_entry():
    source = _extract_block()
    assert "build_backend_entry()" in source


# ---------------------------------------------------------------------------
# T1 JSON-value cases (auth check made moot via an existing, non-empty
# AGENT_TOKENS -- these cases care about the entry shape, not the warning)
# ---------------------------------------------------------------------------

def test_credentialed_private_ok_writes_true(tmp_path):
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("private_ok\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["private_ok"] is True
    assert "roles" not in entry
    assert entry["token_env"] == "DEEPSEEK_API_KEY"


def test_credentialed_roles_writes_exact_array_no_private_ok_key(tmp_path):
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("roles\nextract judge\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert sorted(entry["roles"]) == ["extract", "judge"]
    assert "private_ok" not in entry
    # H2 fix round: "never serves role-less traffic" is TRUE on the
    # CREDENTIALED roles path (effective private_ok defaults false there) --
    # must be printed here.
    assert "never serves role-less" in proc.stderr


def test_credentialed_single_role_writes_exact_array(tmp_path):
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("roles\nextract\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["roles"] == ["extract"]
    assert "private_ok" not in entry
    # Strict-subset dream-slot caveat must fire for a single role.
    assert "does not count toward dream" in proc.stderr
    # The full-set caveat must NOT fire for a single role.


def test_credentialed_full_roleset_no_subset_caveat(tmp_path):
    """H1 fix round: the caveat sentence spans two echo lines ("...does not
    count toward dream\\n  slots -- ..."), so the assertion below must match
    a substring that is actually contiguous on ONE line -- "does not count
    toward dream slots" (with a literal space where the real text has a
    newline) can never appear even when the caveat DOES fire, which made
    the original version of this test vacuous. "does not count toward
    dream" has no line break in it either way, so it is a faithful presence/
    absence probe. See HANDOFF.md for the mutation check proving this one
    actually bites."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("roles\nextract judge\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    assert "does not count toward dream" not in proc.stderr


def test_credentialed_refusing_both_reasks_no_fatal_shape(tmp_path):
    """An answer that is neither 'private_ok' nor 'roles' must re-ask, never
    fall through to a broken/empty shape. Exhausted stdin after one bad
    answer must fail loudly (nonzero exit) and emit NOTHING on stdout --
    never a half-built entry."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("maybe\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "private_ok" in proc.stderr or "roles" in proc.stderr  # re-ask guidance


def test_credentialed_reasks_then_succeeds(tmp_path):
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("maybe\nprivate_ok\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["private_ok"] is True


@pytest.mark.parametrize("roles_input", [",", "summarize", "bogus", "summarize bogus"])
def test_role_input_summarize_or_invalid_reasks_never_written(tmp_path, roles_input):
    """A garbage/invalid/reserved answer to the roles sub-prompt must
    re-ask rather than ever writing an empty or invalid roles list. A lone
    "," parses to ZERO role tokens (it normalises to whitespace) without
    being the literal blank-Enter line that the FINDING in HANDOFF.md
    documents as the (interpreted) DEFAULT-to-both shortcut -- `read`
    itself strips a purely-whitespace line down to empty, so a bare space
    is indistinguishable from Enter and cannot exercise this path; see
    test_blank_role_input_defaults_to_both for that one. This test pairs
    each bad answer with a valid follow-up to prove the loop recovers
    rather than merely failing."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run(f"roles\n{roles_input}\nextract\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["roles"] == ["extract"]
    assert "extract judge" in proc.stderr  # the re-ask guidance line


def test_blank_role_input_defaults_to_both(tmp_path):
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("roles\n\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert sorted(entry["roles"]) == ["extract", "judge"]


def test_uncredentialed_general_writes_private_ok_true(tmp_path):
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("y\n", env_file, token_env="")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["private_ok"] is True
    assert "roles" not in entry
    assert "token_env" not in entry


def test_uncredentialed_blank_defaults_yes(tmp_path):
    """Yes is the default for the uncredentialed general-traffic question --
    matches today's effective value (no token_env => private_ok defaults
    true at the gateway)."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("\n", env_file, token_env="")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["private_ok"] is True


def test_uncredentialed_exhausted_stdin_never_defaults_to_private_ok_true(tmp_path):
    """SEC-HIGH fix round: before this fix, exhausted stdin at the
    uncredentialed general-traffic prompt left `v` empty, the regex
    `[[ ! "$v" =~ ^[Nn]$ ]]` matched (empty does not match ^[Nn]$), and
    yesno_y silently returned TRUE -- writing private_ok:true with no
    operator answer at all. An unanswered access question must never widen
    access: exhausted stdin here must fail loudly, write NOTHING to stdout,
    and must NOT be indistinguishable from a real "yes"."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("", env_file, token_env="")  # closed stdin, not even a blank line
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert '"private_ok":true' not in proc.stdout.replace(" ", "")
    assert "refusing to guess" in proc.stderr


def test_uncredentialed_no_writes_roles_and_honest_caveat(tmp_path):
    """H2 fix round: the "never serves role-less traffic" claim is FALSE on
    THIS path -- _role_eligible ignores `roles` entirely for role-less
    traffic and falls back to the effective private_ok, which defaults TRUE
    for an uncredentialed backend. That sentence must be ABSENT here even
    though it is correctly present on the credentialed roles path (see
    test_credentialed_roles_writes_exact_array_no_private_ok_key) -- only
    the honest "still routes role-less traffic" correction belongs on this
    path."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    proc = _run("n\nextract\n", env_file, token_env="")
    assert proc.returncode == 0, proc.stderr
    import json
    entry = json.loads(proc.stdout)
    assert entry["roles"] == ["extract"]
    assert "private_ok" not in entry
    # The honest default-deny-not-built-yet correction is specific to the
    # uncredentialed roles path.
    assert "still routes" in proc.stderr
    assert "role-less" in proc.stderr
    # The FALSE claim must never appear here.
    assert "never serves role-less" not in proc.stderr


def test_never_writes_private_ok_false(tmp_path):
    """There is no answer sequence that makes this script write
    private_ok: false -- the M-5 choice only ever writes true or omits the
    key; the uncredentialed general-traffic choice only ever writes true or
    omits the key."""
    env_file = tmp_path / "auth_on.env"
    env_file.write_text("AGENT_TOKENS=some-real-token\n")
    for stdin in ("private_ok\n", "roles\nextract judge\n"):
        proc = _run(stdin, env_file, token_env="DEEPSEEK_API_KEY")
        assert proc.returncode == 0, proc.stderr
        assert '"private_ok":false' not in proc.stdout.replace(" ", "")
    for stdin in ("y\n", "n\nextract\n"):
        proc = _run(stdin, env_file, token_env="")
        assert proc.returncode == 0, proc.stderr
        assert '"private_ok":false' not in proc.stdout.replace(" ", "")


# ---------------------------------------------------------------------------
# Auth-warning cases (value-sensitive ENV_FILE, per the brief)
# ---------------------------------------------------------------------------

def test_no_agent_tokens_line_warns(tmp_path):
    env_file = tmp_path / "no_tokens.env"
    env_file.write_text("SOME_OTHER_KEY=1\n")
    proc = _run("private_ok\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    assert "AGENT_TOKENS is not set" in proc.stderr
    assert "bootstrap_tokens.sh" in proc.stderr
    assert "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS" in proc.stderr


def test_agent_tokens_empty_value_warns(tmp_path):
    """A cleared `AGENT_TOKENS=` line is auth-OFF too (the gateway keys on
    bool(_AGENT_TOKENS)) -- must still warn."""
    env_file = tmp_path / "empty_tokens.env"
    env_file.write_text("AGENT_TOKENS=\n")
    proc = _run("private_ok\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    assert "AGENT_TOKENS is not set" in proc.stderr


def test_agent_tokens_set_no_warning(tmp_path):
    env_file = tmp_path / "tokens.env"
    env_file.write_text("AGENT_TOKENS=x\n")
    proc = _run("private_ok\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc.returncode == 0, proc.stderr
    assert "AGENT_TOKENS is not set" not in proc.stderr


def test_uncredentialed_backend_never_triggers_auth_warning(tmp_path):
    """S-05 only cares about credentialed backends -- an uncredentialed one
    must never print the auth-off warning even when AGENT_TOKENS is unset."""
    env_file = tmp_path / "no_tokens.env"
    env_file.write_text("SOME_OTHER_KEY=1\n")
    proc = _run("y\n", env_file, token_env="")
    assert proc.returncode == 0, proc.stderr
    assert "AGENT_TOKENS is not set" not in proc.stderr


def test_auth_off_roles_path_adds_p5_caveat(tmp_path):
    """The extra P-5 caveat is specific to the ROLES choice (P-5 keys on the
    effective private_ok map, which a private_ok:true entry never trips) --
    must NOT appear on the private_ok:true path even when auth is off."""
    env_file = tmp_path / "no_tokens.env"
    env_file.write_text("SOME_OTHER_KEY=1\n")

    proc_roles = _run("roles\nextract judge\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc_roles.returncode == 0, proc_roles.stderr
    assert "P-5 matches this entry too" in proc_roles.stderr

    proc_private_ok = _run("private_ok\n", env_file, token_env="DEEPSEEK_API_KEY")
    assert proc_private_ok.returncode == 0, proc_private_ok.stderr
    assert "P-5 matches this entry too" not in proc_private_ok.stderr
