"""The install-friction guards: the --reveal TTY refusal, the digest-shaped-token
refusal, and the write/full role alias.

⛔ THESE TESTS NEVER INVOKE generate_tokens.py's ENTRY POINT, because its mint
writes into the REAL $HOME and rotates live client tokens (fact:1471) — verifying
the TTY guard by running the real command is what rotated all four skill tokens
while this change was being built. The guards are called as FUNCTIONS, and a test
that needs argument parsing uses a subprocess with HOME on tmp_path and a flag
combination that returns before any mint.

Each test names the failure it kills, per decision:2671 (an isolation test is
allowed only where an adversarial pass has named the failure first).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "shared-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import generate_tokens as gt  # noqa: E402


# ── P0: a revealed token only ever goes to a human's own terminal ────────────
# NAMED FAILURE (measured twice, fact:1499 and fact:1543): a live bearer token
# reaches an agent transcript or a world-readable log by following the published
# install path. A rotation fixes a leaked token; nothing un-writes a transcript.
# MUTATION: make _reveal_output_is_a_terminal return True unconditionally.

def test_a_pipe_is_not_a_terminal(monkeypatch):
    """The case that actually happened: output captured by something."""
    monkeypatch.delenv(gt._REVEAL_TTY_OVERRIDE, raising=False)
    monkeypatch.setattr(sys, "stdout", type("F", (), {"isatty": lambda self: False})())
    assert gt._reveal_output_is_a_terminal() is False


def test_a_terminal_is_a_terminal(monkeypatch):
    monkeypatch.delenv(gt._REVEAL_TTY_OVERRIDE, raising=False)
    monkeypatch.setattr(sys, "stdout", type("T", (), {"isatty": lambda self: True})())
    assert gt._reveal_output_is_a_terminal() is True


def test_a_stdout_that_cannot_answer_is_not_a_terminal(monkeypatch):
    """Fail closed: an object with no usable isatty must not read as a terminal."""
    monkeypatch.delenv(gt._REVEAL_TTY_OVERRIDE, raising=False)

    class Broken:
        def isatty(self):
            raise OSError("detached")

    monkeypatch.setattr(sys, "stdout", Broken())
    assert gt._reveal_output_is_a_terminal() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_the_named_override_is_honoured(monkeypatch, value):
    """A deliberate scripted reveal on a host with no terminal stays possible,
    but only by naming the override, which shows up in a shell history."""
    monkeypatch.setenv(gt._REVEAL_TTY_OVERRIDE, value)
    monkeypatch.setattr(sys, "stdout", type("F", (), {"isatty": lambda self: False})())
    assert gt._reveal_output_is_a_terminal() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_a_non_affirmative_override_does_not_unlock_it(monkeypatch, value):
    """Anything other than a clear yes leaves the guard in place, so a stray or
    emptied environment variable cannot silently re-open the hole."""
    monkeypatch.setenv(gt._REVEAL_TTY_OVERRIDE, value)
    monkeypatch.setattr(sys, "stdout", type("F", (), {"isatty": lambda self: False})())
    assert gt._reveal_output_is_a_terminal() is False


def test_reveal_refuses_end_to_end_without_minting(tmp_path):
    """The whole refusal, through real argument parsing, with HOME redirected so a
    regression that reaches the mint cannot touch the operator's own files.

    The assertion that matters is not only the exit code: it is that NOTHING was
    written under the fake HOME. A guard that refuses after minting would still
    have rotated tokens."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop(gt._REVEAL_TTY_OVERRIDE, None)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--reveal", "codex"],
                       capture_output=True, text=True, env=env, timeout=60,
                       cwd=str(SCRIPTS))
    assert r.returncode == 1, f"expected a refusal, got {r.returncode}: {r.stdout[:400]}"
    assert "REFUSED" in r.stderr
    assert "not a terminal" in r.stderr
    assert sorted(p.name for p in tmp_path.rglob("*")) == before, (
        "the refusal wrote something under HOME — it must refuse BEFORE the mint")
    assert "sha256:" not in r.stdout, "a refused reveal must not print a registry line"


def test_the_refusal_never_names_its_own_bypass(tmp_path):
    """⛔ fact:2055 A-1, ruled into this repo by decision:2058: an agent reading a
    refusal "fixes the error" by prepending the marker, which is why gitguard's
    refusal never names SM_GIT_MAIN_OK. The first cut of this guard printed the
    override variable, handing an agent the exact way to leak the credential the
    guard exists to protect. The refusal still has to tell a human where to look,
    so it points at .env.example by name without naming the variable."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop(gt._REVEAL_TTY_OVERRIDE, None)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--reveal", "codex"],
                       capture_output=True, text=True, env=env, timeout=60,
                       cwd=str(SCRIPTS))
    assert gt._REVEAL_TTY_OVERRIDE not in r.stderr, (
        "the refusal prints the name of its own override — an agent will set it")
    assert gt._REVEAL_TTY_OVERRIDE not in r.stdout
    assert ".env.example" in r.stderr, "a human still needs to be told where it is"
    assert "re-mint" in r.stderr, "someone reading this has usually already leaked one"


# ── P1: a digest is not a token ──────────────────────────────────────────────
# NAMED FAILURE (fact:1543): a client .env was given the sha256 DIGEST from the
# gateway's AGENT_TOKENS line instead of the plaintext token. Nothing validated
# the shape, it was accepted, and it failed only later at the gateway as an
# opaque rejection — diagnosing which is what sent an agent hunting credentials
# across the filesystem.
# MUTATION: make _looks_like_a_digest return False unconditionally.

def test_a_sha256_digest_is_recognised():
    assert gt._looks_like_a_digest("a" * 64) is True
    assert gt._looks_like_a_digest("0123456789abcdef" * 4) is True


def test_an_uppercase_digest_is_recognised():
    """A pasted digest may arrive upper-cased."""
    assert gt._looks_like_a_digest("A" * 64) is True


def test_surrounding_whitespace_does_not_hide_a_digest():
    assert gt._looks_like_a_digest("  " + "a" * 64 + "\n") is True


def test_a_real_token_is_not_mistaken_for_a_digest():
    """token_urlsafe output is 43 characters and carries non-hex characters, so
    the two shapes do not overlap. A false positive here would refuse a VALID
    token, which is worse than the defect being fixed."""
    import secrets
    for _ in range(200):
        assert gt._looks_like_a_digest(secrets.token_urlsafe(32)) is False


@pytest.mark.parametrize("value", [
    "a" * 63,            # one short
    "a" * 65,            # one long
    "g" * 64,            # right length, not hex
    "a" * 32,            # md5-shaped
    "",
])
def test_only_the_exact_digest_shape_is_refused(value):
    """Deliberately narrow: this must not become a general length heuristic."""
    assert gt._looks_like_a_digest(value) is False


# ── P2: /health says "write", the flag must accept it ───────────────────────
# NAMED FAILURE: an operator reads a live role off GET /health, which renders the
# "full" role as "write", passes it back to --role, and is refused for using the
# framework's own word. Hit during a d9400 rebuild.
# MUTATION: remove "write" from the choices list.

def test_write_is_accepted_as_a_role_and_means_full(tmp_path):
    """Parsed, aliased, and NOT refused. Driven through a flag combination that
    returns before any mint: --digest reads a token on stdin and prints a digest."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--role", "write", "--digest", "someagent"],
                       input="x" * 40, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert "invalid choice" not in r.stderr, (
        "--role write was refused; /health renders the full role as write, so the "
        "framework's own vocabulary must be accepted")
    assert r.returncode == 0, r.stderr[:300]


@pytest.mark.parametrize("role", ["read", "full", "admin"])
def test_the_documented_roles_still_parse(role, tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--role", role, "--digest", "someagent"],
                       input="x" * 40, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert "invalid choice" not in r.stderr
    assert r.returncode == 0


def test_an_invented_role_is_still_refused(tmp_path):
    """The alias must not turn the role list into a free-text field."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--role", "superuser", "--digest", "someagent"],
                       input="x" * 40, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert r.returncode != 0
    assert "invalid choice" in r.stderr


# ── P3: an undeliverable mint names its own recovery ────────────────────────
# NAMED FAILURE: the report says a token could not be delivered and leaves the
# reader to work out the fix. Measured on a d9400 rebuild: AGENT_INSTALLS came
# back empty, nothing on the host could authenticate, and the message said only
# "go fix the underlying issue and re-run".

def test_the_missing_directory_failure_names_the_commands_to_fix_it():
    src = (SCRIPTS / "generate_tokens.py").read_text()
    assert "mkdir -p {skill_dir} && bootstrap_tokens.sh --remint {a}" in src, (
        "the missing-directory failure must name both the mkdir and the per-agent "
        "remint, or the reader is left to infer the recovery")


def test_the_refusal_path_also_names_the_mkdir():
    src = (SCRIPTS / "generate_tokens.py").read_text()
    assert "Create it first: mkdir -p {skill_dir}" in src


# ── the documentation the operator has to act on ────────────────────────────
# NAMED FAILURE: a restore brings back LLM_BACKENDS_JSON, the separate _FILE
# pointer line is forgotten, and the backend loads with has_credential False
# while looking correctly configured.

def test_operate_md_says_a_credentialed_restore_needs_both_lines():
    text = (Path(__file__).parent.parent / "OPERATE.md").read_text()
    assert "_FILE=" in text
    assert "has_credential=False" in text, (
        "OPERATE.md must name the symptom of a missing key-file pointer, not just "
        "the command that captures it")


def test_operate_md_warns_not_to_use_the_override_to_get_reveal_working_over_ssh():
    text = (Path(__file__).parent.parent / "OPERATE.md").read_text()
    assert "refuses when stdout is not a terminal" in text


# ── the agent install path must stay usable ─────────────────────────────────
# NAMED FAILURE: the TTY guard is written so broadly that it also blocks the
# WRITE-THROUGH mint, which is how every local agent actually receives its token
# (OPERATE.md Phase 8: --add <agent> --install-path <dir>/.env, which writes the
# token into that agent's own .env at mode 600 and prints only the path). Reveal
# is only for an identity with no local directory, which is inherently an
# operator step. If this test fails, the guard has broken the install.
# MUTATION: drop the `args.reveal and` conjunct so the guard fires unconditionally.

def test_the_guard_is_conditioned_on_reveal_and_nothing_else():
    """A mint with no --reveal must not consult the terminal at all."""
    src = (SCRIPTS / "generate_tokens.py").read_text()
    assert "if args.reveal and not _reveal_output_is_a_terminal():" in src, (
        "the TTY guard must be conditioned on --reveal; unconditioned it would "
        "block the write-through mint that every local agent install uses")


def test_a_non_reveal_invocation_is_unaffected_by_a_non_terminal_stdout(tmp_path):
    """Piped stdout, no --reveal: the guard must not fire. Driven through a flag
    combination that returns before any mint, so nothing is written anywhere."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop(gt._REVEAL_TTY_OVERRIDE, None)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--digest", "someagent"],
                       input="x" * 40, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert r.returncode == 0, r.stderr[:300]
    assert "REFUSED" not in r.stderr
    assert "not a terminal" not in r.stderr
    assert "someagent:sha256:" in r.stdout


def test_write_normalises_to_full():
    """The alias must MAP, not merely parse. Extracted so a mutation can reach it:
    an alias that parses without mapping would send "write" on to agent_roles,
    which rejects it before minting — loud, but a half-built alias all the same."""
    assert gt._normalise_role("write") == "full"


@pytest.mark.parametrize("role", ["read", "full", "admin", None])
def test_normalise_role_leaves_every_real_role_alone(role):
    assert gt._normalise_role(role) == role


def test_write_is_not_itself_a_registry_role():
    """Why the mapping has to exist: the registry vocabulary has no "write"."""
    from agent_roles import VALID_ROLES
    assert "write" not in VALID_ROLES
    assert gt._normalise_role("write") in VALID_ROLES


def test_main_normalises_the_role_all_the_way_into_the_registry(tmp_path, monkeypatch):
    """The effect, not the call site: --role write must reach AGENT_ROLES as "full".

    Isolated the way this suite already isolates mint() -- a freshly loaded module
    whose _DEFAULT_GATEWAY_ENV is a tmp file and whose LOCAL_SKILL_ENV_PATHS is
    emptied, which is the mechanism built after the 2026-08-24 incident where three
    tests calling mint() bare rotated a real agent's token file (fact:1471). Nothing
    here can reach the operator's own ~/.claude.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gt_role_norm_mod", str(SCRIPTS / "generate_tokens.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    gateway_env = tmp_path / "gateway.env"
    gateway_env.write_text("")
    mod._DEFAULT_GATEWAY_ENV = str(gateway_env)
    mod.LOCAL_SKILL_ENV_PATHS = {}
    skill = tmp_path / "skill"
    skill.mkdir()

    # What is observable: agent_roles validates the role BEFORE minting and raises
    # on anything outside read|full|admin, so an alias that parses without mapping
    # makes this add FAIL. rc 0 therefore proves the mapping happened. There is no
    # AGENT_ROLES entry to inspect for "full" — absence of an entry IS full — which
    # is why the assertion is on the outcome rather than on a registry line.
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(["--add", "roletestagent", "--role", "write",
                       "--install-path", str(skill / ".env")])
    out = buf.getvalue()
    assert rc == 0, (
        "--role write was refused all the way through: the alias parsed but did not "
        f"map to a registry role, so agent_roles rejected it. Output: {out[:300]}")
    assert "roletestagent:write" not in out, (
        "the unmapped alias reached the registry — write is not a registry role")
    assert (skill / ".env").exists(), "the token was not delivered write-through"


def test_the_digest_path_refuses_a_digest_end_to_end(tmp_path):
    """The CALL SITE, not just the predicate: a 64-hex value piped into --digest
    must be refused. Without this, disabling the check at its call site leaves the
    predicate's own tests green — which is how a dead guard survives."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--digest", "someagent"],
                       input="a" * 64, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert r.returncode == 1, f"a digest was accepted as a token: {r.stdout[:200]}"
    assert "sha256 DIGEST" in r.stderr
    assert "sha256:" not in r.stdout, "it printed a registry line for a digest"


def test_force_hex_registers_a_genuine_hex_token(tmp_path):
    """The escape hatch the review asked for: a token an operator generated as 64
    hex characters stays registrable, so the guard cannot lock anyone out."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--digest", "someagent", "--force-hex"],
                       input="a" * 64, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert r.returncode == 0, r.stderr[:300]
    assert "someagent:sha256:" in r.stdout


def test_the_digest_refusal_names_force_hex_and_that_is_deliberate(tmp_path):
    """⚠ Read with test_the_refusal_never_names_its_own_bypass, which forbids the
    opposite. The distinction is the kind of escape: --force-hex recovers from a
    MISTAKE for a human at a terminal running an undocumented advanced flag, while
    the TTY override is a SECURITY bypass whose whole risk is an agent using it to
    print a credential. Do not make these two "consistent"."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPTS / "generate_tokens.py"),
                        "--digest", "someagent"],
                       input="a" * 64, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(SCRIPTS))
    assert "--force-hex" in r.stderr
