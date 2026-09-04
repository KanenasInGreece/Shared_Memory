"""S16g (HYG round, R-G'): the SAME balanced-pair quote rule, independently
duplicated in three parsers that must never import from one another (Group
1: the client/server surface split) — the gateway parser (secure_env.py),
the CLI client's manual fallback (memory_bridge.py), and the MCP client's
manual fallback (mcp/vector-skill.py).

Rule under test: a value wrapped in ONE balanced pair of surrounding quotes
— `"v"` or `'v'` — has that pair stripped. Everything else is kept
VERBATIM: `K=v`, `K=`, `K="unbalanced` (kept as-is, quote and all), a `"`
embedded inside the value with no surrounding pair (kept).

Each parser is exercised through its own file-path knob against a `tmp_path`
file — never a real `.env` (fact:1499 / this round's common rules) — using
plain, non-secret-classified key names so the value lands somewhere directly
inspectable (os.environ) rather than behind secret-resolution machinery this
file has no need to re-test.

The CLI (memory_bridge.py) and the MCP client (mcp/vector-skill.py) are both
loaded FRESH per case via importlib-by-path (the same idiom
tests/test_client_secret_mirror_parity.py already uses for vector-skill.py's
dashed filename) rather than `import`/`reload` of the single shared module
object every other test file in this suite also imports — this keeps every
case isolated and leaves no mutated module state for a test that runs later
in the same session to trip over.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)
_MEMORY_BRIDGE_PATH = os.path.join(_HERE, "..", "shared-memory", "scripts", "memory_bridge.py")
_SECURE_ENV_PATH = os.path.join(_HERE, "..", "shared-memory", "scripts", "secure_env.py")
_VECTOR_SKILL_PATH = os.path.join(_HERE, "..", "mcp", "vector-skill.py")

import sys  # noqa: E402
sys.path.insert(0, os.path.join(_HERE, "..", "shared-memory", "scripts"))
import secure_env  # noqa: E402

# Warm up fastmcp's own real (successful) import of `dotenv` — vector-
# skill.py imports fastmcp, which transitively imports pydantic-settings,
# which imports `dotenv` itself at import time. If that chain has never run
# yet when a later test in this file poisons sys.modules["dotenv"] = None
# (to force memory_bridge.py's / vector-skill.py's OWN `from dotenv import
# dotenv_values` into its manual fallback), fastmcp's unrelated import of
# the same name breaks too — this import, done once up front while dotenv
# is still real, makes the poisoning below safe regardless of test order or
# which subset of this file's tests actually runs.
import fastmcp  # noqa: E402,F401

_load_counter = 0


def _fresh_module(path: str):
    """A brand-new module object each call, never registered in
    sys.modules, so repeated loads with a different env-path knob never
    collide and never leak into any other test file's own import of the
    same module by name."""
    global _load_counter
    _load_counter += 1
    spec = importlib.util.spec_from_file_location(
        f"_env_quoting_probe_{_load_counter}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The corpus every parser is checked against. `K=v` / `K=` are the baseline
# (nothing to strip); `K_UNBALANCED` and `K_INNER` prove the negative (kept
# VERBATIM); `K_DQUOTE`/`K_SQUOTE` are the positive (one balanced pair, of
# each quote character, stripped).
_ENV_FILE_BODY = (
    "K_PLAIN=hello\n"
    "K_EMPTY=\n"
    'K_UNBALANCED="unbalanced\n'
    'K_DQUOTE="v with spaces"\n'
    "K_SQUOTE='v with spaces'\n"
    'K_INNER=a"b\n'
)

_EXPECTED = {
    "K_PLAIN": "hello",
    "K_EMPTY": "",
    "K_UNBALANCED": '"unbalanced',
    "K_DQUOTE": "v with spaces",
    "K_SQUOTE": "v with spaces",
    "K_INNER": 'a"b',
}


def _write_env_file(tmp_path) -> str:
    env_file = tmp_path / "probe.env"
    env_file.write_text(_ENV_FILE_BODY)
    return str(env_file)


# ── secure_env.py — the gateway parser ───────────────────────────────────────

def test_secure_env_strips_balanced_quote_pairs(tmp_path, monkeypatch, capsys):
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    try:
        secure_env.load_split_env()
        for key, expected in _EXPECTED.items():
            assert os.environ.get(key) == expected, (
                f"secure_env.py: {key} got {os.environ.get(key)!r}, "
                f"expected {expected!r}"
            )
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


def test_secure_env_warns_by_key_name_only_when_a_pair_was_stripped(
    tmp_path, monkeypatch, capsys
):
    """The warning names the KEY, never the value — and fires only for the
    two cases that actually had a pair stripped."""
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    try:
        secure_env.load_split_env()
        err = capsys.readouterr().err
        assert "K_DQUOTE" in err
        assert "K_SQUOTE" in err
        assert "K_PLAIN" not in err
        assert "K_EMPTY" not in err
        assert "K_UNBALANCED" not in err
        assert "K_INNER" not in err
        assert "v with spaces" not in err, "the warning must never carry the value"
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


# Both memory_bridge.py and vector-skill.py try `from dotenv import
# dotenv_values` FIRST and fall back to their own manual parser only on
# ImportError — and this repo's own test/dev environment has python-dotenv
# installed (measured: dotenv_values() is what actually ran here before this
# was forced off, confirmed by dotenv's own "could not parse statement"
# warning on the K_UNBALANCED line). The fix this lane owns is the MANUAL
# fallback (brief's ":240"/":230" — both inside the manual functions), so
# these two tests force the ImportError branch the documented way: seed
# sys.modules["dotenv"] = None before exec, which makes any `import dotenv`
# raise ImportError immediately (monkeypatch auto-reverts it after the
# test). See tests/test_env_quoting.py's presence in the S2_client handoff
# for the measured finding this uncovered: dotenv_values() itself already
# satisfies S16g for balanced pairs and inner quotes, but not for an
# unbalanced leading quote — it drops that line AND cascades to drop the
# NEXT key/value pair too, rather than keeping the line verbatim.

# ── memory_bridge.py — the CLI client's manual fallback ─────────────────────

def test_cli_manual_parser_strips_balanced_quote_pairs(tmp_path, monkeypatch):
    # Both `module.os.environ` and this process's `os.environ` are the SAME
    # dict (the `os` module is a process-wide singleton) — the module under
    # probe exports into it as a side effect of being loaded, so it must be
    # cleaned up explicitly, the same as the secure_env.py cases above.
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    monkeypatch.setitem(sys.modules, "dotenv", None)
    try:
        module = _fresh_module(_MEMORY_BRIDGE_PATH)
        for key, expected in _EXPECTED.items():
            assert module.os.environ.get(key) == expected, (
                f"memory_bridge.py: {key} got "
                f"{module.os.environ.get(key)!r}, expected {expected!r}"
            )
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


# ── mcp/vector-skill.py — the MCP client's manual fallback ──────────────────

def test_mcp_manual_parser_strips_balanced_quote_pairs(tmp_path, monkeypatch):
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VECTOR_SKILL_ENV", env_path)
    monkeypatch.setitem(sys.modules, "dotenv", None)
    try:
        module = _fresh_module(_VECTOR_SKILL_PATH)
        for key, expected in _EXPECTED.items():
            assert module.os.environ.get(key) == expected, (
                f"mcp/vector-skill.py: {key} got "
                f"{module.os.environ.get(key)!r}, expected {expected!r}"
            )
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)
