"""Credential_Custody_Plan_2026-08-14, PR A2 — generate_tokens.py's mint flow.

RULED (Operator, 2026-08-14): no secret token value is ever printed to
stdout. Covers:
  - mint(): write-through to a LOCAL agent's skill .env (mode 600, no
    create-then-chmod window), nothing printed for a remote/not-installed
    agent, and — the invariant the mutation check targets — no minted
    token value anywhere in captured stdout.
  - --reveal: prints ONLY the named agent's raw token, and only when asked.
  - --convert-digests: rewrites an existing plaintext AGENT_TOKENS line to
    digest form in place, idempotent, no token value printed.
"""
import contextlib
import hashlib
import importlib.util
import io
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def load_generate_tokens():
    """Fresh module each call -- LOCAL_SKILL_ENV_PATHS is mutated per-test.

    ISOLATION IS THE LOADER'S JOB, not per-test discipline (measured 2026-08-24:
    three tests calling mint() bare resolved _DEFAULT_GATEWAY_ENV to the REAL
    gateway .env on a configured machine, unioned the LIVE registry through
    _resolve_roster, and ROTATED a real agent's token file at its real
    AGENT_INSTALLS path -- the fact:1471 hazard reached through the test suite.
    A test that needs a registry sets _DEFAULT_GATEWAY_ENV to a tmp_path file
    explicitly; the default here must be a path that CANNOT exist)."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "shared-memory", "scripts", "generate_tokens.py",
    )
    spec = importlib.util.spec_from_file_location("generate_tokens_test_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._DEFAULT_GATEWAY_ENV = "/nonexistent/generate-tokens-test-gateway.env"
    return mod


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


# ── mint(): write-through + nothing-on-stdout ────────────────────────────────

def test_mint_writes_local_agent_token_file_mode_600(tmp_path):
    gt = load_generate_tokens()
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(claude_dir / ".env")}

    (tokens, digests, _failures), _out = _capture(gt.mint)

    env_file = claude_dir / ".env"
    assert env_file.exists()
    assert f"AGENT_TOKEN={tokens['claude']}" in env_file.read_text()
    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    assert mode == 0o600


def test_mint_never_prints_any_minted_token_value(tmp_path):
    gt = load_generate_tokens()
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(claude_dir / ".env")}

    (tokens, digests, _failures), out = _capture(gt.mint)

    for name, token in tokens.items():
        assert token not in out, f"{name}'s raw token leaked into stdout"
    # Digests ARE expected on stdout -- they are not secret.
    for name, digest in digests.items():
        assert digest in out


def test_mint_prints_digest_form_agent_tokens_line(tmp_path):
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    (tokens, digests, _failures), out = _capture(gt.mint)
    line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    for name in gt.AGENTS:
        assert f"{name}:sha256:{digests[name]}" in line


def test_mint_no_local_path_reports_remote_not_local_install(tmp_path):
    """A remote agent must be reported as remote, and the report must not hand
    the operator a command that rotates the fleet.

    ⛔ THIS TEST USED TO ASSERT `generate_tokens.py --reveal <name>` APPEARED —
    it was pinning the defect. This file's own docstring says --reveal shows a
    token only from the SAME invocation, so that command run afterwards is a
    FULL ROTATION of every agent. The guidance printed beside a freshly minted
    credential told the operator to destroy every other agent's token, and read
    like a retrieval step while doing it.
    """
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}  # nobody is local
    _tokens, out = _capture(gt.mint)
    for name in gt.AGENTS:
        assert "REMOTE" in out
        # The operator must learn the token was registered but not delivered.
        assert "NOT DELIVERED" in out
    # ...and must NOT be told to run the fleet-rotating command.
    for name in gt.AGENTS:
        assert f"generate_tokens.py --reveal {name}" not in out, (
            "the report offers a bare --reveal as a retrieval step; run later "
            "that is a full rotation of every agent")


def test_mint_names_every_undeliverable_agent_in_one_block(tmp_path):
    """A credential nobody can obtain must not be findable only by reading
    twenty lines of per-agent report. Absence from this block is what "every
    agent can authenticate" looks like."""
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    _tokens, out = _capture(gt.mint)

    assert "REGISTERED BUT UNDELIVERABLE" in out
    for name in gt.AGENTS:
        assert name in out.split("REGISTERED BUT UNDELIVERABLE", 1)[1]
    assert "--remint" in out, "no recovery path offered that avoids a fleet rotation"


def test_mint_does_not_flag_a_revealed_agent_as_undeliverable(tmp_path):
    """The counterweight: an agent revealed on this run HAS been delivered, and
    listing it would train the operator to ignore the block.

    ⚠ Names a REMOTE agent that is actually on the default roster
    (`lm_studio`). This test used to say "monitor", which stopped being on the
    roster when the undeliverable-by-default defect was fixed — leaving the
    assertion true for the wrong reason (a name that is never processed at all
    can hardly be listed) and the test vacuous."""
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    _tokens, out = _capture(gt.mint, revealing=["lm_studio"])

    tail = out.split("REGISTERED BUT UNDELIVERABLE", 1)
    assert len(tail) > 1, (
        "the other remote agents on the roster were minted without --reveal, "
        "so the block must be printed — otherwise this test proves nothing"
    )
    block = tail[1].split("Fix now with", 1)[0]
    assert "lm_studio" not in block


def test_mint_preserves_other_keys_in_existing_env_file(tmp_path):
    gt = load_generate_tokens()
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    env_path = claude_dir / ".env"
    env_path.write_text("COORDINATOR_URL=http://localhost:8888\nAGENT_TOKEN=tok_stale\n")
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(env_path)}

    (tokens, _digests, _failures), _out = _capture(gt.mint)

    content = env_path.read_text()
    assert "COORDINATOR_URL=http://localhost:8888" in content
    assert "tok_stale" not in content
    assert f"AGENT_TOKEN={tokens['claude']}" in content


# ── finding 4: mode tightened BEFORE write; symlinks refused ────────────────

def test_write_agent_token_file_tightens_mode_before_writing_content(tmp_path, monkeypatch):
    """The entire measured population at review time was a pre-existing 0644
    skill .env -- os.open()'s mode argument only applies at CREATE, so the
    old code wrote the token first and chmod'd 600 after, leaving it
    world-readable for the whole write. fchmod must happen before the first
    byte of content."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("")
    os.chmod(env_path, 0o644)

    call_order: list[str] = []
    real_fchmod = os.fchmod

    def _tracking_fchmod(fd, mode):
        call_order.append("fchmod")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", _tracking_fchmod)

    real_fdopen = os.fdopen

    def _tracking_fdopen(fd, *a, **kw):
        f = real_fdopen(fd, *a, **kw)
        real_write = f.write

        def _tracking_write(data):
            call_order.append("write")
            return real_write(data)

        f.write = _tracking_write
        return f

    monkeypatch.setattr(os, "fdopen", _tracking_fdopen)

    ok = gt._write_agent_token_file(str(env_path), "tok_new")

    assert ok is True
    assert call_order == ["fchmod", "write"], (
        "mode must be tightened to 600 BEFORE any content is written"
    )
    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600


def test_write_agent_token_file_refuses_symlink(tmp_path):
    """Same-uid agents are adversarial in this framework's threat model
    (S-01/S-10) -- writing a live bearer token through a symlink, which
    could point anywhere another process placed it, must be refused, not
    followed."""
    gt = load_generate_tokens()
    target = tmp_path / "real_target.env"
    target.write_text("SOME=1\n")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    link_path = skill_dir / ".env"
    os.symlink(target, link_path)

    with pytest.raises(gt.AgentEnvIsSymlink):
        gt._write_agent_token_file(str(link_path), "tok_x")

    assert "AGENT_TOKEN" not in target.read_text()


def test_mint_refuses_symlinked_local_env_and_continues_other_agents(tmp_path):
    gt = load_generate_tokens()
    claude_target = tmp_path / "claude_real.env"
    claude_target.write_text("")
    claude_skill_dir = tmp_path / "claude_skill"
    claude_skill_dir.mkdir()
    os.symlink(claude_target, claude_skill_dir / ".env")

    codex_dir = tmp_path / "codex_skill"
    codex_dir.mkdir()

    gt.LOCAL_SKILL_ENV_PATHS = {
        "claude": str(claude_skill_dir / ".env"),
        "codex": str(codex_dir / ".env"),
    }

    (tokens, _digests, _failures), out = _capture(gt.mint)

    refused_lines = [l for l in out.splitlines() if "REFUSED" in l]
    assert len(refused_lines) == 1
    assert "claude" in refused_lines[0]
    assert "AGENT_TOKEN" not in claude_target.read_text()
    # The refusal must not stop the rest of the mint -- codex still gets written.
    assert f"AGENT_TOKEN={tokens['codex']}" in (codex_dir / ".env").read_text()


# ── --reveal: prints only when invoked, and only the named agent ────────────

def test_main_without_reveal_prints_no_token_value(tmp_path):
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    rc, out = _capture(gt.main, [])
    assert rc == 0
    assert "AGENT_TOKEN=" not in out  # only AGENT_TOKENS= (the digest line) may appear
    assert "REVEALING" not in out


def test_main_reveal_prints_only_the_named_agent_token(tmp_path):
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    rc, out = _capture(gt.main, ["--reveal", "codex"])
    assert rc == 0
    assert "REVEALING" in out
    reveal_lines = [l for l in out.splitlines() if l.strip().startswith("codex: AGENT_TOKEN=")]
    assert len(reveal_lines) == 1
    # No OTHER agent's raw token appears anywhere in the output.
    for name in gt.AGENTS:
        if name == "codex":
            continue
        assert f"{name}: AGENT_TOKEN=" not in out


def test_main_reveal_unknown_agent_errors_without_minting(tmp_path, capsys):
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    rc = gt.main(["--reveal", "nonexistent_agent"])
    assert rc == 1


# ── --convert-digests: idempotent, in place, no token value printed ─────────

def test_convert_digests_rewrites_plaintext_to_digest_form(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:tok_abc123,gemini:tok_xyz789\nOTHER=1\n")

    rc, out = _capture(gt.convert_digests, str(env_path))

    assert rc == 0
    content = env_path.read_text()
    assert "tok_abc123" not in content
    assert "tok_xyz789" not in content
    assert f"claude:sha256:{_digest('tok_abc123')}" in content
    assert f"gemini:sha256:{_digest('tok_xyz789')}" in content
    assert "OTHER=1" in content
    assert "tok_abc123" not in out and "tok_xyz789" not in out


def test_convert_digests_is_idempotent(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:tok_abc123\n")

    gt.convert_digests(str(env_path))
    first_pass = env_path.read_text()
    gt.convert_digests(str(env_path))
    second_pass = env_path.read_text()

    assert first_pass == second_pass


def test_convert_digests_mixed_registry_leaves_digest_entries_untouched(tmp_path):
    gt = load_generate_tokens()
    existing_digest = _digest("tok_already_digest")
    env_path = tmp_path / ".env"
    env_path.write_text(f"AGENT_TOKENS=claude:sha256:{existing_digest},gemini:tok_plain\n")

    gt.convert_digests(str(env_path))
    content = env_path.read_text()

    assert f"claude:sha256:{existing_digest}" in content
    assert f"gemini:sha256:{_digest('tok_plain')}" in content
    assert "tok_plain" not in content.replace(f"gemini:sha256:{_digest('tok_plain')}", "")


def test_convert_digests_missing_file_returns_error(tmp_path):
    gt = load_generate_tokens()
    rc, _out = _capture(gt.convert_digests, str(tmp_path / "nope.env"))
    assert rc == 1


def test_convert_digests_no_agent_tokens_line_returns_error(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=1\n")
    rc, _out = _capture(gt.convert_digests, str(env_path))
    assert rc == 1


# ── finding 3: atomic write + abort-on-malformed ─────────────────────────────

def test_convert_digests_malformed_entry_aborts_without_writing_anything(tmp_path):
    """Required fix (finding 3): a malformed AGENT_TOKENS entry must abort
    the WHOLE operation before a single byte is written -- this file also
    holds PG_PASSWORD/NEO4J_PASSWORD/every provider key, so a "converted"
    file that silently dropped a registry entry would lock that agent out
    with no record of why."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    original = "AGENT_TOKENS=claude:tok_abc,bad_entry_no_colon\nOTHER=1\n"
    env_path.write_text(original)

    rc, err = _capture(gt.convert_digests, str(env_path))

    assert rc == 1
    assert env_path.read_text() == original, "an aborted conversion must not touch the file"


def test_convert_digests_result_file_is_mode_600_regardless_of_original_mode(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:tok_abc\n")
    os.chmod(env_path, 0o644)

    gt.convert_digests(str(env_path))

    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600


def test_convert_digests_writes_through_a_temp_file_in_the_same_directory(tmp_path, monkeypatch):
    """Atomicity means: temp file, same dir (same filesystem, so the final
    rename is atomic), fsync'd, then renamed over the original -- never a
    truncate-in-place on the live file."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:tok_abc\n")

    seen_tmp_dirs = []
    real_mkstemp = gt.tempfile.mkstemp

    def _tracking_mkstemp(*a, **kw):
        seen_tmp_dirs.append(kw.get("dir"))
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(gt.tempfile, "mkstemp", _tracking_mkstemp)

    rc = gt.convert_digests(str(env_path))

    assert rc == 0
    assert seen_tmp_dirs == [str(tmp_path)]


# ── finding 12: match AGENT_TOKENS= after a full strip, like the gateway ────

def test_convert_digests_matches_leading_whitespace_agent_tokens_line(tmp_path):
    """The gateway's own loader (secure_env.load_split_env) full-strips each
    line before matching a key, so a leading-whitespace AGENT_TOKENS= line
    still refuses startup on a plaintext entry. convert_digests used to
    right-strip only, so it reported "no AGENT_TOKENS= line found" on
    exactly the line the gateway would refuse to start on."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("  AGENT_TOKENS=claude:tok_abc123\nOTHER=1\n")

    rc, _out = _capture(gt.convert_digests, str(env_path))

    assert rc == 0
    content = env_path.read_text()
    assert f"AGENT_TOKENS=claude:sha256:{_digest('tok_abc123')}" in content
    assert "OTHER=1" in content


# ── --digest: operator-supplied token, from stdin, mints/writes nothing ─────

def test_digest_flag_prints_only_the_digest_entry(monkeypatch):
    gt = load_generate_tokens()
    monkeypatch.setattr(gt.sys, "stdin", io.StringIO("tok_operator_chosen"))

    rc, out = _capture(gt.main, ["--digest", "backup"])

    assert rc == 0
    assert out.strip() == f"backup:sha256:{_digest('tok_operator_chosen')}"


def test_digest_flag_strips_surrounding_whitespace_from_stdin(monkeypatch):
    gt = load_generate_tokens()
    monkeypatch.setattr(gt.sys, "stdin", io.StringIO("  tok_operator_chosen  \n"))

    rc, out = _capture(gt.main, ["--digest", "backup"])

    assert rc == 0
    assert out.strip() == f"backup:sha256:{_digest('tok_operator_chosen')}"


def test_digest_flag_mints_nothing_and_writes_nothing(monkeypatch, tmp_path):
    gt = load_generate_tokens()
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(claude_dir / ".env")}
    monkeypatch.setattr(gt.sys, "stdin", io.StringIO("tok_x"))

    _capture(gt.main, ["--digest", "backup"])

    assert not (claude_dir / ".env").exists(), "--digest must never write through to a skill .env"


def test_digest_flag_empty_stdin_errors_without_printing_a_digest(monkeypatch):
    gt = load_generate_tokens()
    monkeypatch.setattr(gt.sys, "stdin", io.StringIO(""))

    rc, out = _capture(gt.main, ["--digest", "backup"])

    assert rc == 1
    assert "sha256" not in out


# ── Install-path registry + additive mint (fresh-host findings D19-D21) ─────
#
# Every fixture here uses a tmp_path "gateway .env" -- never the real
# shared-memory/.env -- so these tests never depend on, or race, anything
# under an actual install (CLAUDE.md Group 4's "run against the live DB"
# obligation is about SQL; this registry lives entirely in a plaintext file
# this script owns the parsing of end to end).


def test_mint_refuses_registered_path_with_missing_directory(tmp_path):
    """D19: a REGISTERED install path whose directory does not exist yet
    (a fresh host, before the skill package is installed) must REFUSE
    outright -- no token minted, no digest registered, nothing written --
    rather than the old behaviour (silently discard the plaintext while the
    digest still lands in AGENT_TOKENS, leaving an entry nobody can ever
    satisfy short of rotating every agent)."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"  # no gateway .env at all -- first bootstrap
    missing_dir_path = str(tmp_path / "claude_skill" / ".env")  # never mkdir'd
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": missing_dir_path}

    (tokens, digests, _failures), out = _capture(gt.mint, env_path=str(env_path))

    assert "claude" not in tokens
    assert "claude" not in digests
    refused = [l for l in out.splitlines() if "REFUSED" in l and "claude" in l]
    assert len(refused) == 1
    assert str(tmp_path / "claude_skill") in refused[0]
    assert not os.path.exists(missing_dir_path)


def test_mint_installs_line_excludes_a_refused_agent(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    missing_dir_path = str(tmp_path / "claude_skill" / ".env")
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": missing_dir_path}

    _tokens, out = _capture(gt.mint, env_path=str(env_path))

    installs_line = next(l for l in out.splitlines() if l.startswith("AGENT_INSTALLS="))
    assert "claude" not in installs_line


def test_mint_infers_nothing_once_a_registry_is_present(tmp_path):
    """I-A3: once an AGENT_INSTALLS registry line exists (even with zero
    entries), LOCAL_SKILL_ENV_PATHS's guessed defaults must never be
    consulted again -- an agent absent from the registry is REMOTE, full
    stop, even when its name is one this script could easily have guessed a
    path for."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_INSTALLS=\n")  # registry present, but empty
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    # LOCAL_SKILL_ENV_PATHS still names a perfectly good, EXISTING directory
    # for claude -- if mint() were still guessing from it, this would write.
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(claude_dir / ".env")}

    (_tokens, _digests, _failures), out = _capture(gt.mint, env_path=str(env_path))

    assert not (claude_dir / ".env").exists(), "a present registry must never fall back to guessing"
    claude_report_line = next(l for l in out.splitlines() if l.strip().startswith("claude"))
    assert "REMOTE" in claude_report_line


def _capture_err(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


def test_add_mints_one_agent_and_leaves_others_byte_identical(tmp_path):
    """I-A1: an additive mint leaves every OTHER agent's digest in
    AGENT_TOKENS byte-identical -- copied verbatim off disk, never
    recomputed."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    existing_digest = _digest("tok_existing_claude")
    env_path.write_text(f"AGENT_TOKENS=claude:sha256:{existing_digest}\n")

    codex_dir = tmp_path / "codex_skill"
    codex_dir.mkdir()
    codex_env = str(codex_dir / ".env")

    (rc, token), out = _capture(
        gt.add_agent, "codex", install_path=codex_env, env_path=str(env_path),
    )

    assert rc == 0
    assert token is not None
    merged_line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    assert f"claude:sha256:{existing_digest}" in merged_line, "claude's entry must be byte-identical"
    assert f"codex:sha256:{_digest(token)}" in merged_line
    assert f"AGENT_TOKEN={token}" in (codex_dir / ".env").read_text()
    # add_agent must not have MODIFIED the gateway .env itself -- bash does
    # that write, based on the merged line this function prints to stdout.
    assert env_path.read_text() == f"AGENT_TOKENS=claude:sha256:{existing_digest}\n"


def test_add_refuses_when_name_already_registered(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text(f"AGENT_TOKENS=codex:sha256:{_digest('tok_codex')}\n")

    (rc, token), err = _capture_err(
        gt.add_agent, "codex", install_path=None, env_path=str(env_path),
    )

    assert rc == 1
    assert token is None
    assert "already registered" in err


def test_add_refuses_missing_directory_and_mints_nothing(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    missing_path = str(tmp_path / "nope_skill" / ".env")

    (rc, token), err = _capture_err(
        gt.add_agent, "codex", install_path=missing_path, env_path=str(env_path),
    )

    assert rc == 1
    assert token is None
    assert "does not exist" in err
    assert not os.path.exists(missing_path)


# ── L1 (Mint_Flow_Lessons brief): --install-path must be the .env FILE ──────
#
# generate_tokens.py:568 `_write_agent_token_file` computes
# `leaf = os.path.basename(path)` then `os.rename(tmp_name, leaf, ...)`. A
# directory-shaped --install-path (trailing slash, or a path that names a
# directory) makes `leaf` empty, and `os.rename(tmp, "")` raised a cryptic
# FileNotFoundError AFTER a temp file was already created -- a live footgun,
# measured on a real MCP-agent mint. Fixed with a fail-fast check in
# add_agent(), before anything is minted or written, plus a defensive guard
# in the shared writer itself.

def test_add_refuses_directory_shaped_install_path_trailing_slash(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    skill_dir = tmp_path / "shared-memory-mcp"
    skill_dir.mkdir()
    dir_shaped_path = str(skill_dir) + "/"
    before = set(skill_dir.glob("*"))

    (rc, token), err = _capture_err(
        gt.add_agent, "codex", install_path=dir_shaped_path, env_path=str(env_path),
    )

    assert rc == 1
    assert token is None
    assert "must be the .env FILE, not a directory" in err
    after = set(skill_dir.glob("*"))
    assert after == before, f"nothing should have been written: {after - before}"
    assert not env_path.exists(), "the gateway .env is never touched by add_agent()"


def test_add_refuses_directory_shaped_install_path_existing_directory(tmp_path):
    """No trailing slash this time -- `os.path.basename` is non-empty
    (`"shared-memory-mcp"`), so this exercises the SECOND guard clause:
    the path exists and IS a directory."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    skill_dir = tmp_path / "shared-memory-mcp"
    skill_dir.mkdir()
    dir_shaped_path = str(skill_dir)  # no trailing slash
    before = set(skill_dir.glob("*"))

    (rc, token), err = _capture_err(
        gt.add_agent, "codex", install_path=dir_shaped_path, env_path=str(env_path),
    )

    assert rc == 1
    assert token is None
    assert "must be the .env FILE, not a directory" in err
    after = set(skill_dir.glob("*"))
    assert after == before, f"nothing should have been written: {after - before}"


def test_add_refuses_trailing_slash_install_path_to_a_nonexistent_directory(tmp_path):
    """MF1 fix-round regression (security+QA review, reproduced live): a
    trailing-slash --install-path to a directory that does NOT exist yet
    used to pass BOTH clauses of the up-front guard -- the OLD check called
    os.path.basename(install_path.rstrip("/")), and stripping the trailing
    slash before taking the basename recovers a non-empty leaf name (e.g.
    "newdir"), while os.path.isdir() is False for a path that doesn't
    exist. The refusal never fired; the mint proceeded, and
    _write_agent_token_file()'s OWN defensive guard (which checks
    os.path.basename() WITHOUT stripping) caught it instead -- as an
    UNCAUGHT ValueError, since add_agent()'s call site only catches
    AgentEnvIsSymlink/OSError around that write. A crash, not the clean
    rc=1 refusal every other invalid --install-path gets. Fixed by
    dropping the .rstrip("/") in the up-front check."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    nonexistent_dir_path = str(tmp_path / "newdir") + "/"  # "newdir" is never created
    before = set(tmp_path.glob("*"))

    (rc, token), err = _capture_err(
        gt.add_agent, "codex", install_path=nonexistent_dir_path, env_path=str(env_path),
    )

    assert rc == 1
    assert token is None
    assert "must be the .env FILE, not a directory" in err
    after = set(tmp_path.glob("*"))
    assert after == before, f"nothing should have been written: {after - before}"
    assert not os.path.exists(str(tmp_path / "newdir"))


def test_add_directory_shaped_install_path_prints_no_registry_lines(tmp_path):
    """The refusal fires before ANY registry line is computed -- stdout
    must carry neither a merged AGENT_TOKENS= nor AGENT_INSTALLS= line."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    skill_dir = tmp_path / "shared-memory-mcp"
    skill_dir.mkdir()

    (rc, token), out = _capture(
        gt.add_agent, "codex", install_path=str(skill_dir) + "/", env_path=str(env_path),
    )

    assert rc == 1
    assert "AGENT_TOKENS=" not in out
    assert "AGENT_INSTALLS=" not in out


def test_write_agent_token_file_refuses_directory_shaped_path_directly(tmp_path):
    """Defensive guard in the shared writer itself (_write_agent_token_file)
    -- exercised directly, bypassing add_agent()'s own guard, since this
    function is the shared writer for every mint path (bulk mint too)."""
    gt = load_generate_tokens()
    skill_dir = tmp_path / "shared-memory-mcp"
    skill_dir.mkdir()
    before = set(skill_dir.glob("*"))

    with pytest.raises(ValueError, match="directory"):
        gt._write_agent_token_file(str(skill_dir) + "/", "tok_never_used_placeholder")

    after = set(skill_dir.glob("*"))
    assert after == before, f"no temp file should have been created: {after - before}"


def test_add_refuses_shared_path_that_would_clobber_a_live_token(tmp_path):
    """I-A2 (second clause): two agents MAY legitimately share an install
    path (one tool reading another's skill directory), but a write-through
    mint into a path another REGISTERED agent already holds a LIVE token at
    would clobber it (_write_agent_token_file replaces any existing
    AGENT_TOKEN= line wholesale) -- refuse, naming both agents, rather than
    silently overwriting a working credential."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    shared_dir = tmp_path / "shared_skill"
    shared_dir.mkdir()
    shared_env = str(shared_dir / ".env")
    env_path.write_text(
        f"AGENT_TOKENS=codex:sha256:{_digest('tok_codex')}\n"
        f"AGENT_INSTALLS=codex:{shared_env}\n",
    )

    (rc, token), err = _capture_err(
        gt.add_agent, "grok", install_path=shared_env, env_path=str(env_path),
    )

    assert rc == 1
    assert token is None
    assert "codex" in err and "grok" in err
    assert not os.path.exists(shared_env), "refused before writing anything"


def test_add_allows_a_shared_path_when_the_other_agent_has_no_live_token_there(tmp_path):
    """A shared path is refused only when the OTHER agent registered there
    actually holds a LIVE AGENT_TOKENS entry -- an AGENT_INSTALLS entry with
    no matching token (e.g. left over from a D19 refusal, which never
    registers one) is not "live" and must not block a legitimate
    registration."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    shared_dir = tmp_path / "shared_skill"
    shared_dir.mkdir()
    shared_env = str(shared_dir / ".env")
    # AGENT_INSTALLS names "codex" at this path, but AGENT_TOKENS does NOT --
    # codex was never actually minted/registered here (e.g. a stale entry).
    env_path.write_text(f"AGENT_INSTALLS=codex:{shared_env}\n")

    (rc, token), _out = _capture(
        gt.add_agent, "grok", install_path=shared_env, env_path=str(env_path),
    )

    assert rc == 0
    assert token is not None


def test_add_never_prints_the_minted_token_value(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    codex_dir = tmp_path / "codex_skill"
    codex_dir.mkdir()

    (rc, token), out = _capture(
        gt.add_agent, "codex", install_path=str(codex_dir / ".env"), env_path=str(env_path),
    )

    assert rc == 0
    assert token not in out


def test_resolve_roster_includes_a_previously_added_agent(tmp_path):
    """A bulk mint (bootstrap_tokens.sh --force, a full rotation) must roll
    in every name already registered, not just the fixed default AGENTS
    list -- otherwise an agent added later via --add is silently dropped
    from the next rotation and quietly stops being trusted."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('t1')},cursor:sha256:{_digest('t2')}\n",
    )

    roster = gt._resolve_roster(str(env_path))

    assert "cursor" in roster
    for name in gt.AGENTS:
        assert name in roster


def test_reveal_accepts_a_previously_added_agent_outside_the_default_roster(tmp_path, monkeypatch):
    """--reveal must stop refusing names outside the OLD hardcoded AGENTS
    list -- it now accepts any REGISTERED name, resolved from the roster
    (AGENTS union the on-disk registry), not the fixed AGENTS constant."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text(f"AGENT_TOKENS=cursor:sha256:{_digest('t1')}\n")
    monkeypatch.setattr(gt, "_DEFAULT_GATEWAY_ENV", str(env_path))
    gt.LOCAL_SKILL_ENV_PATHS = {}

    rc, out = _capture(gt.main, ["--reveal", "cursor"])

    assert rc == 0
    assert "REVEALING" in out
    reveal_lines = [l for l in out.splitlines() if l.strip().startswith("cursor: AGENT_TOKEN=")]
    assert len(reveal_lines) == 1


def test_add_flag_via_main_prints_no_token_and_writes_through(tmp_path, monkeypatch):
    """End-to-end through main(), the CLI surface bootstrap_tokens.sh --add
    actually invokes."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    monkeypatch.setattr(gt, "_DEFAULT_GATEWAY_ENV", str(env_path))
    codex_dir = tmp_path / "codex_skill"
    codex_dir.mkdir()
    codex_env = str(codex_dir / ".env")

    rc, out = _capture(gt.main, ["--add", "codex", "--install-path", codex_env])

    assert rc == 0
    assert "AGENT_TOKEN=" not in out  # no plaintext without --reveal
    content = (codex_dir / ".env").read_text()
    assert content.startswith("AGENT_TOKEN=tok_")


def test_add_flag_refuses_reveal_for_a_different_name(tmp_path, monkeypatch):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    monkeypatch.setattr(gt, "_DEFAULT_GATEWAY_ENV", str(env_path))

    rc = gt.main(["--add", "codex", "--reveal", "someone_else"])

    assert rc == 1


# ── L2 (Mint_Flow_Lessons brief): re-mint/add reminds to re-init the client ──
#
# A token is read from its .env file ONCE, at import (vector-skill.py's
# _AGENT_TOKEN_FROM_FILE and memory_bridge.py's own load both populate it at
# load time). Re-minting rotates the registered digest immediately, so an
# ALREADY-RUNNING client keeps presenting the previous token until it
# re-reads the file -- every request, reads included, 401s until then. This
# was invisible: the mint printed the written path and merged registry
# lines, but never said so. Wording differs by install kind (refined live,
# 2026-09-01): an MCP install needs its MCP SERVER respawned (a full host
# restart, or a per-server reload/disable-enable where the host offers
# one -- a full restart is not required), while a CLI skill install needs
# its process restarted outright.

def test_remint_success_prints_mcp_server_respawn_reminder(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    mcp_dir = tmp_path / "shared-memory-mcp"
    mcp_dir.mkdir()
    mcp_env = str(mcp_dir / ".env")
    mcp_env_path = mcp_dir / ".env"
    mcp_env_path.write_text("AGENT_TOKEN=tok_old_value\n")
    env_path.write_text(
        f"AGENT_TOKENS=codex:sha256:{_digest('tok_old_value')}\n"
        f"AGENT_INSTALLS=codex:mcp:{mcp_env}\n",
    )

    (rc, token), out = _capture(
        gt.add_agent, "codex", install_path=mcp_env, env_path=str(env_path),
        replace=True, install_kind="mcp",
    )

    assert rc == 0
    assert token is not None
    assert "respawn it" in out and "memory MCP server" in out
    assert "already running" in out
    assert "re-reads the rotated" in out
    assert token not in out, "no plaintext without --reveal"


def test_add_success_prints_skill_process_restart_reminder(tmp_path):
    """Default install kind (a CLI skill, not an MCP connector) gets the
    plainer 'restart the process' wording, not the MCP-server framing."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    codex_dir = tmp_path / "codex_skill"
    codex_dir.mkdir()

    (rc, token), out = _capture(
        gt.add_agent, "codex", install_path=str(codex_dir / ".env"), env_path=str(env_path),
    )

    assert rc == 0
    assert "restart it" in out
    assert "re-reads the token" in out
    assert "respawn it" not in out and "memory MCP server" not in out


def test_add_remote_agent_no_install_path_prints_no_respawn_reminder(tmp_path):
    """No write happened (no install path), so there is no local process to
    respawn/restart -- the reminder must not appear."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"

    (rc, token), out = _capture(
        gt.add_agent, "codex", install_path=None, env_path=str(env_path),
    )

    assert rc == 0
    assert "respawn it" not in out
    assert "restart it" not in out


def test_reveal_refusal_path_prints_no_respawn_reminder(tmp_path):
    """A pure --reveal refusal (unknown agent name, bulk-mint surface) never
    reaches add_agent()/mint() at all -- the reminder, which only ever
    prints on a SUCCESSFUL add_agent() call, must not appear."""
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}

    rc, out = _capture(gt.main, ["--reveal", "nonexistent_agent"])

    assert rc == 1
    assert "respawn it" not in out
    assert "restart it" not in out


# ── I-A7: a symlink at ANY component of the registered path is refused ──────
#
# CRITICAL fix (security review, execution-reproduced fix round 2): the
# FIRST cut of the symlink guard applied O_NOFOLLOW to the leaf only.
# os.path.isdir(skill_dir) happily followed a symlinked PARENT directory,
# so a same-uid process that replaced the parent with a symlink defeated
# the guard completely -- the write reported SUCCESS and the live bearer
# token landed in an attacker-controlled directory. These tests reproduce
# that exact scenario (parent symlink) plus a grandparent-level variant,
# against the actual _write_agent_token_file() -- not a reimplementation.

def test_write_agent_token_file_refuses_symlinked_parent_directory(tmp_path):
    """The exact CRITICAL finding: registry path <legit>/shared-memory/.env
    where shared-memory itself is a symlink to an attacker-controlled
    directory. Must refuse -- no exception swallowed into a plain False,
    no write anywhere."""
    gt = load_generate_tokens()
    attacker_dir = tmp_path / "attacker_controlled"
    attacker_dir.mkdir()
    legit_base = tmp_path / "legit"
    legit_base.mkdir()
    symlinked_skill_dir = legit_base / "shared-memory"
    os.symlink(attacker_dir, symlinked_skill_dir)
    attack_path = symlinked_skill_dir / ".env"

    with pytest.raises(gt.AgentEnvIsSymlink):
        gt._write_agent_token_file(str(attack_path), "tok_attack")

    assert list(attacker_dir.iterdir()) == [], "nothing may land in the attacker-controlled directory"


def test_write_agent_token_file_refuses_symlinked_grandparent_directory(tmp_path):
    """A symlink further up the chain than the immediate parent must ALSO
    be refused -- I-A7 says ANY component, not just the leaf's parent."""
    gt = load_generate_tokens()
    attacker_dir = tmp_path / "gp_attacker"
    (attacker_dir / "skills" / "shared-memory").mkdir(parents=True)
    real_base = tmp_path / "gp_real_base"
    real_base.mkdir()
    symlinked_ancestor = real_base / "claude_link"
    os.symlink(attacker_dir, symlinked_ancestor)
    attack_path = symlinked_ancestor / "skills" / "shared-memory" / ".env"

    with pytest.raises(gt.AgentEnvIsSymlink):
        gt._write_agent_token_file(str(attack_path), "tok_attack")

    assert not (attacker_dir / "skills" / "shared-memory" / ".env").exists()


def test_resolve_symlink_free_dir_fd_names_the_offending_component(tmp_path):
    gt = load_generate_tokens()
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    legit_base = tmp_path / "legit"
    legit_base.mkdir()
    link = legit_base / "sm"
    os.symlink(attacker_dir, link)

    with pytest.raises(gt.AgentEnvIsSymlink) as exc_info:
        gt._resolve_symlink_free_dir_fd(str(link))
    assert str(link) in str(exc_info.value)


def test_resolve_symlink_free_dir_fd_missing_directory_raises_file_not_found(tmp_path):
    """A genuinely missing directory must still raise FileNotFoundError
    (D19's "not installed locally" signal), never AgentEnvIsSymlink."""
    gt = load_generate_tokens()
    with pytest.raises(FileNotFoundError):
        gt._resolve_symlink_free_dir_fd(str(tmp_path / "does_not_exist" / "skill"))


# ── F4 atomicity: a mid-write failure leaves the ORIGINAL file untouched ────

def test_write_agent_token_file_atomic_write_leaves_original_untouched_on_failure(
    tmp_path, monkeypatch,
):
    gt = load_generate_tokens()
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    env_path = skill_dir / ".env"
    env_path.write_text("AGENT_TOKEN=tok_original\n")

    def _failing_fsync(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gt.os, "fsync", _failing_fsync)

    with pytest.raises(OSError):
        gt._write_agent_token_file(str(env_path), "tok_should_not_land")

    assert env_path.read_text() == "AGENT_TOKEN=tok_original\n"
    leftovers = [p for p in skill_dir.iterdir() if p.name != ".env"]
    assert leftovers == [], f"no temp file should be left behind: {leftovers}"


# ── I-A8: a delimiter/newline/NUL/whitespace-padded name or path is refused
#    at INPUT, before anything is minted, written, or registered ───────────

def test_validate_registry_field_refuses_comma():
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt._validate_registry_field("a,b", "agent name")


def test_validate_registry_field_refuses_colon():
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt._validate_registry_field("a:b", "agent name")


def test_validate_registry_field_refuses_newline():
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt._validate_registry_field("a\nb", "install path")


def test_validate_registry_field_refuses_nul():
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt._validate_registry_field("a\x00b", "install path")


def test_validate_registry_field_refuses_leading_trailing_whitespace():
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt._validate_registry_field(" codex", "agent name")
    with pytest.raises(ValueError):
        gt._validate_registry_field("codex ", "agent name")


def test_validate_registry_field_accepts_a_clean_name():
    gt = load_generate_tokens()
    gt._validate_registry_field("codex", "agent name")  # must not raise


def test_add_refuses_name_containing_comma_mints_nothing(tmp_path, capsys):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"

    rc, token = gt.add_agent("codex,evil", install_path=None, env_path=str(env_path))

    assert rc == 1
    assert token is None
    assert not env_path.exists()


def test_add_refuses_install_path_containing_comma_reproduces_f2(tmp_path):
    """F2, reproduced verbatim: an install path smuggling a second
    name:path pair via a comma must never reach the printed AGENT_INSTALLS
    line -- add_agent() must refuse before minting or printing anything.

    Asserts on the VALIDATION error message specifically (naming the
    comma), not merely rc == 1 -- any payload containing a comma followed
    by a second "/" also makes os.path.dirname() resolve to a NONEXISTENT
    directory, which the D19 check refuses independently. An earlier draft
    of this test used exactly such a payload and kept passing after a
    mutation that disabled validation entirely -- masked by that other,
    unrelated refusal path. Asserting the message text is what actually
    isolates this guard."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    real_dir = tmp_path / "opencode_skill"
    real_dir.mkdir()
    malicious_path = f"{real_dir}/.env,victim:/attacker/victim.env"

    with contextlib.redirect_stderr(io.StringIO()) as err:
        rc, token = gt.add_agent("opencode", install_path=malicious_path, env_path=str(env_path))

    assert rc == 1
    assert token is None
    assert "delimiter" in err.getvalue()
    assert not env_path.exists()
    assert list(real_dir.iterdir()) == [], "nothing may be written through a malicious path"


def test_add_refuses_install_path_containing_newline_reproduces_f2b(tmp_path):
    """F2b, reproduced verbatim: a newline in an install path must never
    reach the printed AGENT_INSTALLS line, where it would forge a second
    .env assignment (this same file is passed to
    `docker compose --env-file`).

    Uses a REAL, existing directory prefix so this test isolates the
    VALIDATION refusal specifically -- a nonexistent-directory path would
    also be refused by the D19 check, which would let this test pass
    "for the wrong reason" even if validation itself were broken (a
    mutation-check found exactly this gap in an earlier draft: the D19
    path masked the missing validation)."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    real_dir = tmp_path / "opencode_skill"
    real_dir.mkdir()
    malicious_path = f"{real_dir}/.env\nAGENT_TOKENS=attacker:sha256:deadbeef"

    with contextlib.redirect_stderr(io.StringIO()) as err:
        rc, token = gt.add_agent("opencode", install_path=malicious_path, env_path=str(env_path))

    assert rc == 1
    assert token is None
    assert "delimiter" in err.getvalue()
    assert not env_path.exists()
    assert list(real_dir.iterdir()) == [], "nothing may be written through a malicious path"


# ── I-A9: registry paths compared by NORMALIZED identity ────────────────────

def test_same_registered_file_detects_dotdot_aliasing():
    gt = load_generate_tokens()
    a = "/tmp/s/claude/skills/shared-memory/.env"
    b = "/tmp/s/claude/skills/other/../shared-memory/.env"
    assert a != b  # the old literal-equality check would miss this
    assert gt._same_registered_file(a, b) is True


def test_same_registered_file_rejects_genuinely_different_paths():
    gt = load_generate_tokens()
    assert gt._same_registered_file("/tmp/a/.env", "/tmp/b/.env") is False


def test_add_refuses_dotdot_aliased_clobber_reproduces_f3(tmp_path):
    """F3, reproduced verbatim: two spellings of the SAME file must both be
    caught by the clobber check, not just a literal string match."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    shared_dir = tmp_path / "claude" / "skills" / "shared-memory"
    shared_dir.mkdir(parents=True)
    real_path = str(shared_dir / ".env")
    aliased_path = str(tmp_path / "claude" / "skills" / "other" / ".." / "shared-memory" / ".env")
    assert real_path != aliased_path

    env_path.write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_claude')}\n"
        f"AGENT_INSTALLS=claude:{real_path}\n",
    )

    rc, token = gt.add_agent("grok", install_path=aliased_path, env_path=str(env_path))

    assert rc == 1
    assert token is None
    assert not (shared_dir / ".env").exists()


# ── I-A10: a partial bulk-mint failure never revokes a working credential ──

def test_mint_carries_forward_existing_digest_when_write_fails(tmp_path):
    """F4: a ROTATION of an already-registered agent whose write fails this
    round must NOT drop that agent's entry from the printed AGENT_TOKENS
    line -- the OLD digest (still valid against the untouched file) is
    carried forward verbatim, so nothing is revoked."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    existing_digest = _digest("tok_existing_claude")
    env_path.write_text(
        f"AGENT_TOKENS=claude:sha256:{existing_digest}\n"
        f"AGENT_INSTALLS=claude:{tmp_path / 'gone' / '.env'}\n",  # directory never created
    )

    (tokens, digests, failures), out = _capture(gt.mint, env_path=str(env_path))

    assert "claude" not in tokens  # nothing NEW was minted for it
    line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    assert f"claude:sha256:{existing_digest}" in line, "the OLD, still-valid entry must survive"
    assert any(name == "claude" for name, _reason in failures)


def test_mint_omits_first_time_agent_entirely_on_write_failure(tmp_path):
    """The carry-forward only applies to a ROTATION -- an agent with NO
    prior entry that fails on its first mint is correctly omitted (D19's
    original intent: never register a digest nobody holds the plaintext
    for)."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text(f"AGENT_INSTALLS=claude:{tmp_path / 'gone' / '.env'}\n")

    (tokens, digests, failures), out = _capture(gt.mint, env_path=str(env_path))

    assert "claude" not in tokens
    line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    assert "claude" not in line
    assert any(name == "claude" for name, _reason in failures)


def test_mint_prints_partial_failure_block_when_any_agent_fails(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text(f"AGENT_INSTALLS=claude:{tmp_path / 'gone' / '.env'}\n")

    _result, out = _capture(gt.mint, env_path=str(env_path))

    assert "PARTIAL FAILURE" in out
    assert "claude" in out


def test_mint_no_partial_failure_block_when_everything_succeeds(tmp_path):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(claude_dir / ".env")}

    _result, out = _capture(gt.mint, env_path=str(env_path))

    assert "PARTIAL FAILURE" not in out


# ── Least privilege must hold on EVERY mint path ─────────────────────────────
#
# Operator rule (2026-08-23): "monitor always has a READ only token."
#
# It did not. READ_ONLY_AGENTS exists, and the BULK mint printed
# AGENT_ROLES=monitor:read from it — but the ADDITIVE path (add_agent) printed
# only AGENT_TOKENS and AGENT_INSTALLS, and bootstrap_tokens.sh greps for
# exactly the lines it is given. Absence from AGENT_ROLES means FULL read/write
# (coordinator._load_agent_roles), so `--add monitor` minted a write-capable
# token for a dashboard whose own definition says it "must not borrow a
# write-capable agent token". The policy was data in one place and nothing in
# the other.


def test_a_read_only_agent_always_gets_the_read_role():
    """The rule itself, as a pure function — no gateway, no .env, no database.
    That it was never expressible this way is why the gap survived."""
    gt = load_generate_tokens()
    for name in gt.READ_ONLY_AGENTS:
        assert gt.role_for(name) == "read"


def test_an_ordinary_agent_gets_no_role_entry():
    """Full access is the ABSENCE of an entry, not an entry saying 'full'.
    Asserting None here pins that shape: emitting `claude:full` would also
    'work' and would quietly change what an unlisted name means."""
    gt = load_generate_tokens()
    assert gt.role_for("claude") is None


def test_a_read_only_agent_cannot_be_widened_by_an_explicit_role():
    """'Always' has to mean always, or the roster is a default with a bypass."""
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt.role_for("monitor", "full")
    with pytest.raises(ValueError):
        gt.role_for("monitor", "admin")
    # ...but restating the truth is fine.
    assert gt.role_for("monitor", "read") == "read"


def test_an_unknown_role_name_is_refused():
    gt = load_generate_tokens()
    with pytest.raises(ValueError):
        gt.role_for("codex", "superuser")


def test_add_of_a_read_only_agent_emits_the_roles_line(tmp_path, capsys):
    """THE regression. Without this line bootstrap_tokens.sh writes nothing to
    AGENT_ROLES, and the gateway reads absence as full read/write."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n")

    rc, token = gt.add_agent("monitor", install_path=None, env_path=str(env_path))
    out = capsys.readouterr().out

    assert rc == 0 and token
    roles = [l for l in out.split("\n") if l.startswith("AGENT_ROLES=")]
    assert roles, "no AGENT_ROLES line — the additive path is back to full access"
    assert "monitor:read" in roles[0]


def test_add_merges_into_existing_roles_and_never_drops_backup_admin(tmp_path, capsys):
    """A roles line rebuilt from just the new agent would silently widen the
    backup credential — the one token confined to /admin/*. Merge, never replace."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n"
        "AGENT_ROLES=backup:admin\n"
    )

    rc, _ = gt.add_agent("monitor", install_path=None, env_path=str(env_path))
    out = capsys.readouterr().out

    assert rc == 0
    line = next(l for l in out.split("\n") if l.startswith("AGENT_ROLES="))
    assert "backup:admin" in line
    assert "monitor:read" in line


def test_add_of_an_ordinary_agent_emits_no_roles_line(tmp_path, capsys):
    """No entry means full access, and that must stay the DEFAULT shape — an
    --add that started emitting `name:full` for everyone would make the roles
    line grow without ever narrowing anything."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n")

    rc, _ = gt.add_agent("codex", install_path=None, env_path=str(env_path))
    out = capsys.readouterr().out

    assert rc == 0
    assert not [l for l in out.split("\n") if l.startswith("AGENT_ROLES=")]


def test_add_refuses_to_widen_a_read_only_agent_and_mints_nothing(tmp_path, capsys):
    """The refusal must precede the mint, same contract as every other refusal
    in add_agent — a refused --add leaves no trace at all."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n")

    rc, token = gt.add_agent("monitor", install_path=None,
                             env_path=str(env_path), role="full")
    out = capsys.readouterr()

    assert rc == 1
    assert token is None
    assert not [l for l in (out.out + out.err).split("\n")
                if l.startswith("AGENT_TOKENS=")]


# ── RULING 1.3 (fix/uninstall-reverse-and-help) — the REMOTE recovery advice
# printed by a single-agent add_agent() mint (no install_path) must name a
# command that actually WORKS the next time it is run. It used to say
# "generate_tokens.py --add {name} --reveal {name}" -- but by the time that
# line prints, {name} IS ALREADY REGISTERED (this very mint just added or
# re-issued it), so a later --add of the same name hits the
# already-registered refusal (test_add_refuses_when_already_registered
# above) instead of revealing anything. --remint is the one that re-issues
# an EXISTING name -- see test_add_refuses_when_already_registered's own
# error text ("--add never silently rotates ... Use --remint"), which
# already agreed with this and was simply not applied to this one call site.

def test_remote_add_recovery_advice_uses_remint_not_add(tmp_path, capsys):
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n")

    rc, token = gt.add_agent("codex", install_path=None, env_path=str(env_path))
    out = capsys.readouterr().out

    assert rc == 0 and token
    assert "generate_tokens.py --remint codex --reveal codex" in out, (
        f"recovery advice for an undelivered REMOTE agent must name --remint "
        f"(re-issues an EXISTING name), not --add (refuses one):\n{out}"
    )
    assert "generate_tokens.py --add codex --reveal codex" not in out, (
        f"the OLD, broken advice is still being printed -- following it would "
        f"hit the already-registered refusal instead of revealing anything:\n{out}"
    )


def test_following_the_printed_remote_add_recovery_advice_actually_works(tmp_path, monkeypatch):
    """Not just a string match: prove the EXACT command the report prints
    actually succeeds when run through main() -- the real CLI surface
    bootstrap_tokens.sh invokes -- which the OLD --add form did not (it hits
    test_add_refuses_when_already_registered's refusal instead).

    generate_tokens.py itself never writes AGENT_TOKENS= into env_path --
    it only PRINTS the merged line for bootstrap_tokens.sh's own
    replace_registry_lines() to persist (see bootstrap_tokens.sh). This test
    applies that same merged line back to env_path between the two
    invocations, exactly as the real two-script pipeline does, so the second
    call sees codex as genuinely registered.
    """
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    monkeypatch.setattr(gt, "_DEFAULT_GATEWAY_ENV", str(env_path))
    env_path.write_text("AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n")

    # First mint: generate_tokens.py --add codex   (no --install-path -> REMOTE)
    rc, out = _capture(gt.main, ["--add", "codex"])
    assert rc == 0, out
    assert "generate_tokens.py --remint codex --reveal codex" in out

    # bootstrap_tokens.sh's replace_registry_lines() step: persist the merged
    # AGENT_TOKENS= line this mint printed.
    tokens_line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    env_path.write_text(tokens_line + "\n")

    # Now follow the printed advice literally, against the SAME env file:
    # generate_tokens.py --remint codex --reveal codex
    rc2, out2 = _capture(gt.main, ["--remint", "codex", "--reveal", "codex"])
    assert rc2 == 0, (
        f"the printed recovery command FAILED when actually run:\n{out2}"
    )
    assert "codex: AGENT_TOKEN=" in out2, (
        f"--remint codex --reveal codex did not reveal codex's token:\n{out2}"
    )


# ── The default roster must never register a token nobody can receive ────────
#
# MEASURED DEFECT (operator-ruled fix): "monitor" sat on the default AGENTS
# roster, but the monitor dashboard lives in a sibling repo — it has no
# LOCAL_SKILL_ENV_PATHS entry, so it is classified REMOTE, and the documented
# bulk invocation (AGENTS.md Phase 6 / bootstrap_tokens.sh, bare) carries no
# --reveal. So EVERY fresh install minted a monitor token, registered its
# digest in AGENT_TOKENS, and discarded the plaintext at birth — the framework's
# own D19 rule ("never mint a token into a digest registry that nobody actually
# received, which is worse than not minting at all") broken by its default path,
# and unrecoverable by --add afterwards (already registered → refused).
#
# The fix has two halves, and the second is the one that generalises: the
# roster no longer defaults to a name that cannot be delivered, and ANY
# remote-classified agent minted without --reveal is now named UNDELIVERABLE,
# loudly, with the --remint recovery command that rotates nobody else.


def test_default_roster_is_pinned_by_value_and_excludes_the_monitor():
    """Pinned BY VALUE, not by `"monitor" not in AGENTS`: an equality assertion
    against a list literal is what makes a future re-addition of ANY
    undeliverable-by-default name a failing test rather than a silent one."""
    gt = load_generate_tokens()
    assert gt.AGENTS == [
        "claude", "gemini", "grok", "codex", "lm_studio", "antigravity",
    ]


def test_a_fresh_registry_mints_no_monitor(tmp_path):
    """The FRESH-install half: no AGENT_TOKENS on disk at all → the resolved
    bulk roster is exactly the default list, with no monitor in it, so no
    monitor digest can reach a fresh AGENT_TOKENS line."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"          # no gateway .env at all
    gt.LOCAL_SKILL_ENV_PATHS = {}

    roster = gt._resolve_roster(str(env_path))
    assert "monitor" not in roster
    assert roster == gt.AGENTS

    (_tokens, digests, _failures), out = _capture(gt.mint, env_path=str(env_path))

    assert "monitor" not in digests
    tokens_line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    assert "monitor:sha256:" not in tokens_line


def test_an_existing_registry_keeps_monitor_across_a_force_rotation(tmp_path):
    """⚠ THE REGRESSION THIS FIX MUST NOT CAUSE. _resolve_roster() unions the
    fixed AGENTS list with every name already registered in the gateway .env's
    AGENT_TOKENS — precisely so that removing a name from the default list can
    never revoke a credential an existing install is already using. An install
    that already has monitor registered must still get a fresh monitor token
    from a --force rotation (which calls this same bulk mint path).

    Delivered via --reveal here, exactly as the operator would run a rotation
    that includes a remote agent — the point is that the NAME survives the
    roster change, not that a rotation can deliver without --reveal.
    """
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    stale_digest = _digest("tok_monitor_from_an_older_install")
    env_path.write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_claude')},"
        f"monitor:sha256:{stale_digest}\n"
    )
    gt.LOCAL_SKILL_ENV_PATHS = {}

    roster = gt._resolve_roster(str(env_path))
    assert "monitor" in roster, (
        "an already-registered agent was dropped from the rotation roster — "
        "a --force rotation would silently stop trusting it"
    )

    (tokens, digests, _failures), out = _capture(
        gt.mint, env_path=str(env_path), roster=roster, revealing=["monitor"],
    )

    assert "monitor" in tokens, "the rotation minted nothing for a registered agent"
    tokens_line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    assert f"monitor:sha256:{digests['monitor']}" in tokens_line
    assert stale_digest not in tokens_line, "a rotation must issue a FRESH digest"


# ── The undeliverable warning: remote + no --reveal, and nothing else ────────


def test_bulk_mint_warns_undeliverable_for_a_remote_agent_without_reveal(tmp_path):
    """Not monitor-specific: fires for whatever is remote-classified on this
    roster. Asserts the recovery command is the one that WORKS afterwards
    (--remint re-issues an EXISTING name; --add refuses it).

    ⚠ Asserts on the PER-AGENT report specifically — the text BEFORE the
    closing summary block — not on the whole of stdout. A first draft asserted
    `"UNDELIVERABLE" in out`, and a mutation check found it toothless: the
    closing block prints that same word and the same `--remint` command, so
    deleting the per-agent warning entirely left the test green. The two
    surfaces are asserted separately below for exactly that reason.
    """
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    gt.LOCAL_SKILL_ENV_PATHS = {}          # lm_studio is remote, like every name here

    _result, out = _capture(
        gt.mint, env_path=str(env_path), roster=["lm_studio"],
    )

    halves = out.split("REGISTERED BUT UNDELIVERABLE", 1)
    assert len(halves) == 2, "the closing summary block was not printed at all"
    per_agent, closing_block = halves

    assert "UNDELIVERABLE" in per_agent, (
        "a remote agent minted without --reveal was registered silently in the "
        "per-agent report — its digest is in AGENT_TOKENS and its plaintext is "
        "already gone"
    )
    assert "generate_tokens.py --remint lm_studio --reveal lm_studio" in per_agent, (
        "the per-agent warning does not name the recovery command for THIS agent"
    )
    assert "generate_tokens.py --remint lm_studio --reveal lm_studio" in closing_block, (
        "the closing block names no per-agent recovery command — it used to "
        "print a literal '<name>' placeholder"
    )


def test_bulk_mint_does_not_warn_undeliverable_for_a_local_agent(tmp_path):
    """The counterweight, half one: a written-through local agent HAS its
    token. Warning on it would train the operator to scroll past the block."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(claude_dir / ".env")}

    (tokens, _digests, _failures), out = _capture(
        gt.mint, env_path=str(env_path), roster=["claude"],
    )

    assert f"AGENT_TOKEN={tokens['claude']}" in (claude_dir / ".env").read_text()
    assert "UNDELIVERABLE" not in out


def test_bulk_mint_does_not_warn_undeliverable_for_a_remote_agent_with_reveal(tmp_path):
    """The counterweight, half two: --reveal on the SAME invocation IS the
    delivery path for a remote agent, so there is nothing to warn about."""
    gt = load_generate_tokens()
    env_path = tmp_path / ".env"
    gt.LOCAL_SKILL_ENV_PATHS = {}

    _result, out = _capture(
        gt.mint, env_path=str(env_path), roster=["lm_studio"],
        revealing=["lm_studio"],
    )

    assert "UNDELIVERABLE" not in out
