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
