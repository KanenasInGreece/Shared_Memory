"""Credential_Custody_Plan_2026-08-14, PR A2 — generate_tokens.py's mint flow.

RULED (Xenofon, 2026-08-14): no secret token value is ever printed to
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
    """Fresh module each call -- LOCAL_SKILL_ENV_PATHS is mutated per-test."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "shared-memory", "scripts", "generate_tokens.py",
    )
    spec = importlib.util.spec_from_file_location("generate_tokens_test_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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

    (tokens, digests), _out = _capture(gt.mint)

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

    (tokens, digests), out = _capture(gt.mint)

    for name, token in tokens.items():
        assert token not in out, f"{name}'s raw token leaked into stdout"
    # Digests ARE expected on stdout -- they are not secret.
    for name, digest in digests.items():
        assert digest in out


def test_mint_prints_digest_form_agent_tokens_line(tmp_path):
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    (tokens, digests), out = _capture(gt.mint)
    line = next(l for l in out.splitlines() if l.startswith("AGENT_TOKENS="))
    for name in gt.AGENTS:
        assert f"{name}:sha256:{digests[name]}" in line


def test_mint_no_local_path_reports_remote_not_local_install(tmp_path):
    gt = load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}  # nobody is local
    _tokens, out = _capture(gt.mint)
    for name in gt.AGENTS:
        assert f"generate_tokens.py --reveal {name}" in out


def test_mint_preserves_other_keys_in_existing_env_file(tmp_path):
    gt = load_generate_tokens()
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    env_path = claude_dir / ".env"
    env_path.write_text("COORDINATOR_URL=http://localhost:8888\nAGENT_TOKEN=tok_stale\n")
    gt.LOCAL_SKILL_ENV_PATHS = {"claude": str(env_path)}

    (tokens, _digests), _out = _capture(gt.mint)

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

    (tokens, _digests), out = _capture(gt.mint)

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
